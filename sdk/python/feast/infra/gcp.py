import itertools
from datetime import datetime
from multiprocessing.pool import ThreadPool
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import mmh3
import pandas
import pyarrow

from feast import FeatureTable, utils
from feast.data_source import BigQuerySource
from feast.feature_view import FeatureView
from feast.infra.key_encoding_utils import serialize_entity_key
from feast.infra.offline_stores.helpers import get_offline_store_from_sources
from feast.infra.provider import (
    Provider,
    RetrievalJob,
    _convert_arrow_to_proto,
    _get_column_names,
    _run_field_mapping,
)
from feast.protos.feast.types.EntityKey_pb2 import EntityKey as EntityKeyProto
from feast.protos.feast.types.Value_pb2 import Value as ValueProto
from feast.registry import Registry
from feast.repo_config import DatastoreOnlineStoreConfig, RepoConfig


class GcpProvider(Provider):
    _gcp_project_id: Optional[str]

    def __init__(self, config: Optional[DatastoreOnlineStoreConfig]):
        if config:
            self._gcp_project_id = config.project_id
        else:
            self._gcp_project_id = None

    def _initialize_client(self):
        from google.cloud import datastore

        if self._gcp_project_id is not None:
            return datastore.Client(self._gcp_project_id)
        else:
            return datastore.Client()

    def update_infra(
        self,
        project: str,
        tables_to_delete: Sequence[Union[FeatureTable, FeatureView]],
        tables_to_keep: Sequence[Union[FeatureTable, FeatureView]],
        partial: bool,
    ):
        from google.cloud import datastore

        client = self._initialize_client()

        for table in tables_to_keep:
            key = client.key("Project", project, "Table", table.name)
            entity = datastore.Entity(key=key)
            entity.update({"created_ts": datetime.utcnow()})
            client.put(entity)

        for table in tables_to_delete:
            _delete_all_values(
                client, client.key("Project", project, "Table", table.name)
            )

            # Delete the table metadata datastore entity
            key = client.key("Project", project, "Table", table.name)
            client.delete(key)

    def teardown_infra(
        self, project: str, tables: Sequence[Union[FeatureTable, FeatureView]]
    ) -> None:
        client = self._initialize_client()

        for table in tables:
            _delete_all_values(
                client, client.key("Project", project, "Table", table.name)
            )

            # Delete the table metadata datastore entity
            key = client.key("Project", project, "Table", table.name)
            client.delete(key)

    def online_write_batch(
        self,
        project: str,
        table: Union[FeatureTable, FeatureView],
        data: List[
            Tuple[EntityKeyProto, Dict[str, ValueProto], datetime, Optional[datetime]]
        ],
        progress: Optional[Callable[[int], Any]],
    ) -> None:
        client = self._initialize_client()

        pool = ThreadPool(processes=10)
        pool.map(
            lambda b: _write_minibatch(client, project, table, b, progress),
            _to_minibatches(data),
        )

    def online_read(
        self,
        project: str,
        table: Union[FeatureTable, FeatureView],
        entity_keys: List[EntityKeyProto],
    ) -> List[Tuple[Optional[datetime], Optional[Dict[str, ValueProto]]]]:
        client = self._initialize_client()

        result: List[Tuple[Optional[datetime], Optional[Dict[str, ValueProto]]]] = []
        for entity_key in entity_keys:
            document_id = compute_datastore_entity_id(entity_key)
            key = client.key(
                "Project", project, "Table", table.name, "Row", document_id
            )
            value = client.get(key)
            if value is not None:
                res = {}
                for feature_name, value_bin in value["values"].items():
                    val = ValueProto()
                    val.ParseFromString(value_bin)
                    res[feature_name] = val
                result.append((value["event_ts"], res))
            else:
                result.append((None, None))
        return result

    def materialize_single_feature_view(
        self,
        feature_view: FeatureView,
        start_date: datetime,
        end_date: datetime,
        registry: Registry,
        project: str,
    ) -> None:
        assert isinstance(feature_view.input, BigQuerySource)

        entities = []
        for entity_name in feature_view.entities:
            entities.append(registry.get_entity(entity_name, project))

        (
            join_key_columns,
            feature_name_columns,
            event_timestamp_column,
            created_timestamp_column,
        ) = _get_column_names(feature_view, entities)

        start_date = utils.make_tzaware(start_date)
        end_date = utils.make_tzaware(end_date)

        offline_store = get_offline_store_from_sources([feature_view.input])
        table = offline_store.pull_latest_from_table_or_query(
            data_source=feature_view.input,
            join_key_columns=join_key_columns,
            feature_name_columns=feature_name_columns,
            event_timestamp_column=event_timestamp_column,
            created_timestamp_column=created_timestamp_column,
            start_date=start_date,
            end_date=end_date,
        )

        if feature_view.input.field_mapping is not None:
            table = _run_field_mapping(table, feature_view.input.field_mapping)

        join_keys = [entity.join_key for entity in entities]
        rows_to_write = _convert_arrow_to_proto(table, feature_view, join_keys)

        self.online_write_batch(project, feature_view, rows_to_write, None)

        feature_view.materialization_intervals.append((start_date, end_date))
        registry.apply_feature_view(feature_view, project)

    @staticmethod
    def _pull_query(query: str) -> pyarrow.Table:
        from google.cloud import bigquery

        client = bigquery.Client()
        query_job = client.query(query)
        return query_job.to_arrow()

    @staticmethod
    def get_historical_features(
        config: RepoConfig,
        feature_views: List[FeatureView],
        feature_refs: List[str],
        entity_df: Union[pandas.DataFrame, str],
        registry: Registry,
        project: str,
    ) -> RetrievalJob:
        offline_store = get_offline_store_from_sources(
            [feature_view.input for feature_view in feature_views]
        )
        job = offline_store.get_historical_features(
            config=config,
            feature_views=feature_views,
            feature_refs=feature_refs,
            entity_df=entity_df,
            registry=registry,
            project=project,
        )
        return job


ProtoBatch = Sequence[
    Tuple[EntityKeyProto, Dict[str, ValueProto], datetime, Optional[datetime]]
]


def _to_minibatches(data: ProtoBatch, batch_size=50) -> Iterator[ProtoBatch]:
    """
    Split data into minibatches, making sure we stay under GCP datastore transaction size
    limits.
    """
    iterable = iter(data)

    while True:
        batch = list(itertools.islice(iterable, batch_size))
        if len(batch) > 0:
            yield batch
        else:
            break


def _write_minibatch(
    client,
    project: str,
    table: Union[FeatureTable, FeatureView],
    data: Sequence[
        Tuple[EntityKeyProto, Dict[str, ValueProto], datetime, Optional[datetime]]
    ],
    progress: Optional[Callable[[int], Any]],
):
    from google.api_core.exceptions import Conflict
    from google.cloud import datastore

    num_retries_on_conflict = 3
    row_count = 0
    for retry_number in range(num_retries_on_conflict):
        try:
            row_count = 0
            with client.transaction():
                for entity_key, features, timestamp, created_ts in data:
                    document_id = compute_datastore_entity_id(entity_key)

                    key = client.key(
                        "Project", project, "Table", table.name, "Row", document_id,
                    )

                    entity = client.get(key)
                    if entity is not None:
                        if entity["event_ts"] > utils.make_tzaware(timestamp):
                            # Do not overwrite feature values computed from fresher data
                            continue
                        elif (
                            entity["event_ts"] == utils.make_tzaware(timestamp)
                            and created_ts is not None
                            and entity["created_ts"] is not None
                            and entity["created_ts"] > utils.make_tzaware(created_ts)
                        ):
                            # Do not overwrite feature values computed from the same data, but
                            # computed later than this one
                            continue
                    else:
                        entity = datastore.Entity(key=key)

                    entity.update(
                        dict(
                            key=entity_key.SerializeToString(),
                            values={
                                k: v.SerializeToString() for k, v in features.items()
                            },
                            event_ts=utils.make_tzaware(timestamp),
                            created_ts=(
                                utils.make_tzaware(created_ts)
                                if created_ts is not None
                                else None
                            ),
                        )
                    )
                    client.put(entity)
                    row_count += 1

                    if progress:
                        progress(1)
            break  # make sure to break out of retry loop if all went well
        except Conflict:
            if retry_number == num_retries_on_conflict - 1:
                raise


def _delete_all_values(client, key) -> None:
    """
    Delete all data under the key path in datastore.
    """
    while True:
        query = client.query(kind="Row", ancestor=key)
        entities = list(query.fetch(limit=1000))
        if not entities:
            return

        for entity in entities:
            client.delete(entity.key)


def compute_datastore_entity_id(entity_key: EntityKeyProto) -> str:
    """
    Compute Datastore Entity id given Feast Entity Key.

    Remember that Datastore Entity is a concept from the Datastore data model, that has nothing to
    do with the Entity concept we have in Feast.
    """
    return mmh3.hash_bytes(serialize_entity_key(entity_key)).hex()
