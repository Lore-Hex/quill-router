// Package schema holds the spike's schema, spike.sql, and splits it into the
// statements Spanner's DDL requests take.
package schema

import (
	_ "embed"
	"errors"
	"strings"
)

//go:embed spike.sql
var spikeSQL string

// Statements are spike.sql's statements, in order, without comments.
func Statements() ([]string, error) {
	return Split(spikeSQL)
}

// Split splits GoogleSQL text into its statements at the semicolons outside
// quotes and comments, and drops the comments: `--` and `#` to the end of the
// line, at a newline or a carriage return as GoogleSQL's lexer has it, and
// `/* */`. A quote, single, double, triple or backquoted, runs to
// its closing quote; a backslash escapes the next character in every kind,
// raw strings included. Text that ends inside a quote or a block comment is
// refused rather than split by guess.
func Split(sql string) ([]string, error) {
	var out []string
	var b strings.Builder
	flush := func() {
		if s := strings.TrimSpace(b.String()); s != "" {
			out = append(out, s)
		}
		b.Reset()
	}
	for i := 0; i < len(sql); {
		switch {
		case strings.HasPrefix(sql[i:], "--") || sql[i] == '#':
			end := strings.IndexAny(sql[i:], "\n\r")
			if end < 0 {
				end = len(sql) - i
			}
			i += end
		case strings.HasPrefix(sql[i:], "/*"):
			end := strings.Index(sql[i+2:], "*/")
			if end < 0 {
				return nil, errors.New("schema: a block comment is not closed")
			}
			i += 2 + end + 2
			b.WriteByte(' ')
		case sql[i] == '\'' || sql[i] == '"' || sql[i] == '`':
			delimiter := sql[i : i+1]
			if delimiter != "`" && strings.HasPrefix(sql[i:], strings.Repeat(delimiter, 3)) {
				delimiter = strings.Repeat(delimiter, 3)
			}
			j := i + len(delimiter)
			for {
				if j >= len(sql) {
					return nil, errors.New("schema: a quote is not closed")
				}
				if sql[j] == '\\' {
					j += 2
					continue
				}
				if strings.HasPrefix(sql[j:], delimiter) {
					j += len(delimiter)
					break
				}
				j++
			}
			b.WriteString(sql[i:j])
			i = j
		case sql[i] == ';':
			flush()
			i++
		default:
			b.WriteByte(sql[i])
			i++
		}
	}
	flush()
	return out, nil
}
