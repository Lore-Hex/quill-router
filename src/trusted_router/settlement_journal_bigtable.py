"""Fixed single-cluster Bigtable implementation of the journal row port.

No client is constructed on import. RPC retries are disabled, including stream
restarts: callers must resolve uncertain writes by replaying the state machine.
"""
from __future__ import annotations

import re
from typing import Any

from trusted_router.settlement_journal import META, Compare, JournalError, Read, ReadPage, Request

FAMILY = 'journal'


def validate_profile(profile: Any, *, cluster: str, location: str, region: str) -> None:
    # Inspect the real protobuf oneof; a truthy default single-cluster object
    # is NOT evidence that multi-cluster routing is disabled.
    pb = getattr(profile, '_pb', profile)
    if (pb.WhichOneof('routing_policy') != 'single_cluster_routing' or
            pb.single_cluster_routing.cluster_id != cluster or
            not pb.single_cluster_routing.allow_transactional_writes or
            location.rsplit('/', 1)[-1].rsplit('-', 1)[0] != region):
        raise JournalError('journal requires a transactional single cluster in its region')


class BigtableJournalStorage:
    def __init__(self, data_client: Any, *, table_name: str, app_profile_id: str,
                 region: str, cluster: str, profile: Any, location: str,
                 timeout: float = 0.25) -> None:
        if not app_profile_id or not 0.01 <= timeout <= 10:
            raise ValueError('explicit app profile and bounded timeout required')
        if profile.name != table_name.split('/tables/')[0] + '/appProfiles/' + app_profile_id:
            raise JournalError('app profile identity mismatch')
        validate_profile(profile, cluster=cluster, location=location, region=region)
        self.client = data_client
        self.table_name = table_name
        self.app_profile_id = app_profile_id
        self.region = region
        self.timeout = timeout

    @classmethod
    def connect(cls, *, project: str, instance: str, table: str,
                app_profile_id: str, region: str, cluster: str,
                timeout: float = 0.25) -> BigtableJournalStorage:
        from google.cloud.bigtable import Client

        client = Client(project=project, admin=True)
        inst = client.instance(instance)
        profile = client.instance_admin_client.get_app_profile(
            request={'name': inst.name + '/appProfiles/' + app_profile_id},
            retry=None, timeout=timeout,
        )
        cluster_pb = client.instance_admin_client.get_cluster(
            request={'name': inst.name + '/clusters/' + cluster}, retry=None, timeout=timeout,
        )
        table_name = inst.name + '/tables/' + table
        table_pb = client.table_admin_client.get_table(
            request={'name': table_name, 'view': 2}, retry=None, timeout=timeout,
        )
        family = table_pb.column_families.get(FAMILY)
        if family is None:
            raise JournalError('journal family missing')
        rule = family.gc_rule
        if rule._pb.WhichOneof('rule') != 'max_num_versions' or rule.max_num_versions != 1:
            raise JournalError('journal GC must be maxversions=1 only; no age deletion')
        return cls(client.table_data_client, table_name=table_name,
                   app_profile_id=app_profile_id, region=region, cluster=cluster,
                   profile=profile, location=cluster_pb.location, timeout=timeout)

    def call(self, request: Request) -> Any:
        from google.api_core.retry import Retry
        from google.cloud.bigtable.row_data import PartialRowsData
        from google.cloud.bigtable_v2.types import (
            CheckAndMutateRowRequest,
            ColumnRange,
            Mutation,
            ReadRowsRequest,
            RowFilter,
            RowSet,
        )

        parts = request.key.split(b'#')
        if len(parts) != 5 or parts[1] != b'settlement' or parts[2] != self.region.encode().hex().encode():
            raise JournalError('wrong regional journal row')
        chain = [RowFilter(family_name_regex_filter='^journal$')]
        if isinstance(request, (Read, ReadPage)):
            columns = request.columns if isinstance(request, Read) else META
            if not 1 <= len(columns) <= 68:
                raise ValueError('bounded column selection required')
            expression = b'^(' + b'|'.join(re.escape(c) for c in columns) + b')$'
            selection = RowFilter(column_qualifier_regex_filter=expression)
            if isinstance(request, ReadPage):
                selection = RowFilter(interleave=RowFilter.Interleave(filters=[
                    selection, RowFilter(column_range_filter=ColumnRange(
                        family_name=FAMILY,
                        start_qualifier_closed=f's/{request.start:04x}'.encode(),
                        end_qualifier_open=f's/{request.stop:04x}'.encode(),
                    )),
                ]))
            chain.extend([selection, RowFilter(cells_per_column_limit_filter=1)])
            read = ReadRowsRequest(
                table_name=self.table_name, app_profile_id=self.app_profile_id,
                rows=RowSet(row_keys=[request.key]), rows_limit=1,
                filter=RowFilter(chain=RowFilter.Chain(filters=chain)),
            )

            def read_once(req: Any, **_kwargs: Any) -> Any:
                # PartialRowsData otherwise pads the initial timeout by 1s.
                return self.client.read_rows(req, retry=None, timeout=self.timeout)

            rows = list(PartialRowsData(
                read_once, read, retry=Retry(predicate=lambda _: False, deadline=self.timeout),
            ))
            if not rows:
                return {}
            if len(rows) != 1 or rows[0].row_key != request.key:
                raise JournalError('unexpected point-read result')
            cells = rows[0].cells.get(FAMILY, {})
            return {c: bytes(v[0].value) for c, v in cells.items() if v}
        assert isinstance(request, Compare)
        chain.extend([
            RowFilter(column_qualifier_regex_filter=b'^' + re.escape(request.column) + b'$'),
            RowFilter(cells_per_column_limit_filter=1),
        ])
        if request.expected is not None:
            # Newest THEN value: historical versions must never match a CAS.
            chain.append(RowFilter(value_regex_filter=b'^' + re.escape(request.expected) + b'$'))
        mutations = []
        for column, value in request.updates.items():
            # Delete historical versions atomically with replacement, bounding
            # physical cells even before asynchronous GC and avoiding timestamp ties.
            mutations.append(Mutation(delete_from_column=Mutation.DeleteFromColumn(
                family_name=FAMILY, column_qualifier=column,
            )))
            if value is not None:
                mutations.append(Mutation(set_cell=Mutation.SetCell(
                    family_name=FAMILY, column_qualifier=column,
                    timestamp_micros=-1, value=value,
                )))
        response = self.client.check_and_mutate_row(CheckAndMutateRowRequest(
            table_name=self.table_name, app_profile_id=self.app_profile_id,
            row_key=request.key,
            predicate_filter=RowFilter(chain=RowFilter.Chain(filters=chain)),
            true_mutations=mutations if request.expected is not None else [],
            false_mutations=mutations if request.expected is None else [],
        ), retry=None, timeout=self.timeout)
        matched = bool(response.predicate_matched)
        return matched if request.expected is not None else not matched
