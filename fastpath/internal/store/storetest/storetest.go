// Package storetest runs the store's tests against the Spanner emulator, and
// only against one on this machine: a fresh instance for each test binary,
// deleted when it exits, and databases created from fastpath.sql.
//
// The tests opt in with FASTPATH_SPANNER_EMULATOR=1 and SPANNER_EMULATOR_HOST
// (fastpath/README.md). Without the first they skip, saying why; with it, an
// emulator that cannot be reached fails them, so CI's store job cannot pass
// by skipping.
package storetest

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"os"
	"time"

	"cloud.google.com/go/spanner"
	database "cloud.google.com/go/spanner/admin/database/apiv1"
	"cloud.google.com/go/spanner/admin/database/apiv1/databasepb"
	instance "cloud.google.com/go/spanner/admin/instance/apiv1"
	"cloud.google.com/go/spanner/admin/instance/apiv1/instancepb"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/Lore-Hex/quill-router/fastpath/schema"
)

const project = "fastpath-test"

// Emulator is an instance on the emulator for one test binary.
type Emulator struct {
	host      string
	instance  string
	databases *database.DatabaseAdminClient
	instances *instance.InstanceAdminClient
}

// Start creates the instance. Without FASTPATH_SPANNER_EMULATOR=1 it returns
// no emulator and the reason the tests skip; opted in, it refuses an
// emulator host that is not this machine's, GCP credentials in the
// environment, and an emulator it cannot reach.
func Start(ctx context.Context) (*Emulator, string, error) {
	if os.Getenv("FASTPATH_SPANNER_EMULATOR") != "1" {
		return nil, "the store's tests run against the Spanner emulator: set FASTPATH_SPANNER_EMULATOR=1 and " +
			"SPANNER_EMULATOR_HOST, as fastpath/README.md says", nil
	}
	host := os.Getenv("SPANNER_EMULATOR_HOST")
	address, _, err := net.SplitHostPort(host)
	if err != nil {
		return nil, "", fmt.Errorf("SPANNER_EMULATOR_HOST %q is not host:port: %w", host, err)
	}
	if address != "localhost" && address != "127.0.0.1" && address != "::1" {
		return nil, "", fmt.Errorf("SPANNER_EMULATOR_HOST %q: the emulator must be on this machine", host)
	}
	for _, name := range []string{"GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY_JSON"} {
		if os.Getenv(name) != "" {
			return nil, "", fmt.Errorf("%s is set: the emulator's tests run without GCP credentials", name)
		}
	}
	conn, err := net.DialTimeout("tcp", host, 5*time.Second)
	if err != nil {
		return nil, "", fmt.Errorf("the Spanner emulator at %s cannot be reached: %w", host, err)
	}
	conn.Close()
	e := &Emulator{host: host, instance: "fp-" + randomHex(6)}
	if e.instances, err = instance.NewInstanceAdminClient(ctx, e.options()...); err != nil {
		return nil, "", err
	}
	if e.databases, err = database.NewDatabaseAdminClient(ctx, e.options()...); err != nil {
		e.instances.Close()
		return nil, "", err
	}
	op, err := e.instances.CreateInstance(ctx, &instancepb.CreateInstanceRequest{
		Parent:     "projects/" + project,
		InstanceId: e.instance,
		Instance: &instancepb.Instance{
			Config:      "projects/" + project + "/instanceConfigs/emulator-config",
			DisplayName: e.instance,
			NodeCount:   1,
		},
	})
	if err == nil {
		_, err = op.Wait(ctx)
	}
	if err != nil {
		e.databases.Close()
		e.instances.Close()
		return nil, "", fmt.Errorf("creating instance %s: %w", e.instance, err)
	}
	return e, "", nil
}

// options point a client at the emulator explicitly, without credentials,
// rather than trusting the client library to read SPANNER_EMULATOR_HOST.
func (e *Emulator) options() []option.ClientOption {
	return []option.ClientOption{
		option.WithEndpoint(e.host),
		option.WithoutAuthentication(),
		option.WithGRPCDialOption(grpc.WithTransportCredentials(insecure.NewCredentials())),
	}
}

// Database creates a database from statements, fastpath.sql's when they are
// nil, and returns a client of it; the client closes with the test binary's
// instance, or when its caller closes it.
func (e *Emulator) Database(ctx context.Context, name string, statements []string) (*spanner.Client, error) {
	if statements == nil {
		var err error
		if statements, err = schema.Statements(); err != nil {
			return nil, err
		}
	}
	op, err := e.databases.CreateDatabase(ctx, &databasepb.CreateDatabaseRequest{
		Parent:          "projects/" + project + "/instances/" + e.instance,
		CreateStatement: "CREATE DATABASE `" + name + "`",
		ExtraStatements: statements,
	})
	if err == nil {
		_, err = op.Wait(ctx)
	}
	if err != nil {
		return nil, fmt.Errorf("creating database %s: %w", name, err)
	}
	return spanner.NewClientWithConfig(ctx, e.Path(name), spanner.ClientConfig{DisableNativeMetrics: true}, e.options()...)
}

// Client is another client of a database Database created, with the extra
// options given, such as a counting interceptor; its caller closes it.
func (e *Emulator) Client(ctx context.Context, name string, extra ...option.ClientOption) (*spanner.Client, error) {
	return spanner.NewClientWithConfig(ctx, e.Path(name), spanner.ClientConfig{DisableNativeMetrics: true},
		append(e.options(), extra...)...)
}

// Path is the database's resource name.
func (e *Emulator) Path(name string) string {
	return "projects/" + project + "/instances/" + e.instance + "/databases/" + name
}

// Close deletes the instance and its databases.
func (e *Emulator) Close(ctx context.Context) error {
	err := e.instances.DeleteInstance(ctx, &instancepb.DeleteInstanceRequest{
		Name: "projects/" + project + "/instances/" + e.instance,
	})
	return errors.Join(err, e.databases.Close(), e.instances.Close())
}

// UniqueID is an identifier no other test in the binary uses, so tests share
// a database without seeing one another's rows.
func UniqueID(prefix string) string {
	return prefix + "-" + randomHex(8)
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// Enabled is the mutation that turns the fast path on for a workspace
// (tr_fastpath_workspace), which a test applies with the workspace's credit
// rows so that its grants are taken.
func Enabled(workspace string) *spanner.Mutation {
	return spanner.InsertOrUpdateMap("tr_fastpath_workspace", map[string]any{
		"workspace_id": workspace, "enabled": true, "changed_at": spanner.CommitTimestamp})
}
