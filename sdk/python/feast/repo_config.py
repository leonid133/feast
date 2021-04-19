from pathlib import Path

import yaml
from pydantic import BaseModel, StrictInt, StrictStr, ValidationError, root_validator
from pydantic.error_wrappers import ErrorWrapper
from pydantic.typing import Dict, Literal, Optional, Union


class FeastBaseModel(BaseModel):
    """ Feast Pydantic Configuration Class """

    class Config:
        arbitrary_types_allowed = True
        extra = "forbid"


class SqliteOnlineStoreConfig(FeastBaseModel):
    """ Online store config for local (SQLite-based) store """

    type: Literal["sqlite"] = "sqlite"
    """ Online store type selector"""

    path: StrictStr = "data/online.db"
    """ (optional) Path to sqlite db """


class DatastoreOnlineStoreConfig(FeastBaseModel):
    """ Online store config for GCP Datastore """

    type: Literal["datastore"] = "datastore"
    """ Online store type selector"""

    project_id: Optional[StrictStr] = None
    """ (optional) GCP Project Id """

class DynamoOnlineStoreConfig(FeastBaseModel):
    """Online store config for DynamoDB store"""
    type: Literal["dynamo"] = "dynamo"
    """Online store type selector"""
    project_id: Optional[StrictStr] = None

OnlineStoreConfig = Union[DatastoreOnlineStoreConfig, SqliteOnlineStoreConfig, DynamoOnlineStoreConfig]


class RegistryConfig(FeastBaseModel):
    """ Metadata Store Configuration. Configuration that relates to reading from and writing to the Feast registry."""

    path: StrictStr
    """ str: Path to metadata store. Can be a local path, or remote object storage path, e.g. gcs://foo/bar """

    cache_ttl_seconds: StrictInt = 600
    """int: The cache TTL is the amount of time registry state will be cached in memory. If this TTL is exceeded then
     the registry will be refreshed when any feature store method asks for access to registry state. The TTL can be
     set to infinity by setting TTL to 0 seconds, which means the cache will only be loaded once and will never
     expire. Users can manually refresh the cache by calling feature_store.refresh_registry() """


class RepoConfig(FeastBaseModel):
    """ Repo config. Typically loaded from `feature_store.yaml` """

    registry: Union[StrictStr, RegistryConfig] = "data/registry.db"
    """ str: Path to metadata store. Can be a local path, or remote object storage path, e.g. gcs://foo/bar """

    project: StrictStr
    """ str: Feast project id. This can be any alphanumeric string up to 16 characters.
        You can have multiple independent feature repositories deployed to the same cloud
        provider account, as long as they have different project ids.
    """

    provider: StrictStr
    """ str: local or gcp or aws_dynamo """

    online_store: OnlineStoreConfig = SqliteOnlineStoreConfig()
    """ OnlineStoreConfig: Online store configuration (optional depending on provider) """

    def get_registry_config(self):
        if isinstance(self.registry, str):
            return RegistryConfig(path=self.registry)
        else:
            return self.registry

    @root_validator(pre=True)
    def _validate_online_store_config(cls, values):
        # This method will validate whether the online store configurations are set correctly. This explicit validation
        # is necessary because Pydantic Unions throw very verbose and cryptic exceptions. We also use this method to
        # impute the default online store type based on the selected provider. For the time being this method should be
        # considered tech debt until we can implement https://github.com/samuelcolvin/pydantic/issues/619 or a more
        # granular configuration system

        # Skip if online store isn't set explicitly
        if "online_store" not in values:
            values["online_store"] = dict()

        # Skip if we arent creating the configuration from a dict
        if not isinstance(values["online_store"], Dict):
            return values

        # Make sure that the provider configuration is set. We need it to set the defaults
        assert "provider" in values

        if "online_store" in values:
            # Set the default type
            if "type" not in values["online_store"]:
                if values["provider"] == "local":
                    values["online_store"]["type"] = "sqlite"
                elif values["provider"] == "gcp":
                    values["online_store"]["type"] = "datastore"
                elif values["provider"] == "aws_dynamo":
                    values["online_store"]["type"] = "datastore"
                elif values["provider"] == "aws_dynamo":
                    values["online_store"]["type"] = "dynamo"
            online_store_type = values["online_store"]["type"]
            # Make sure the user hasn't provided the wrong type
            assert online_store_type in ["datastore", "sqlite", "dynamo"]

            # Validate the dict to ensure one of the union types match
            try:
                if online_store_type == "sqlite":
                    SqliteOnlineStoreConfig(**values["online_store"])
                elif online_store_type == "datastore":
                    DatastoreOnlineStoreConfig(**values["online_store"])
                elif online_store_type == "dynamo":
                    print("govno")
                    DynamoOnlineStoreConfig(**values["online_store"])
                else:
                    raise ValidationError(
                        f"Invalid online store type {online_store_type}"
                    )
            except ValidationError as e:
                raise ValidationError(
                    [ErrorWrapper(e, loc="online_store")],
                    model=SqliteOnlineStoreConfig,
                )
        return values


class FeastConfigError(Exception):
    def __init__(self, error_message, config_path):
        self._error_message = error_message
        self._config_path = config_path
        super().__init__(self._error_message)

    def __str__(self) -> str:
        return f"{self._error_message}\nat {self._config_path}"

    def __repr__(self) -> str:
        return (
            f"FeastConfigError({repr(self._error_message)}, {repr(self._config_path)})"
        )


def load_repo_config(repo_path: Path) -> RepoConfig:
    config_path = repo_path / "feature_store.yaml"

    with open(config_path) as f:
        raw_config = yaml.safe_load(f)
        try:
            return RepoConfig(**raw_config)
        except ValidationError as e:
            raise FeastConfigError(e, config_path)
