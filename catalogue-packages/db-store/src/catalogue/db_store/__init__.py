"""Persistence package.

`db.py` (connection/schema/migration core) and `store.py` (write-guarded API) are
submodules here; this facade re-exports their public surface so the long-standing
`from catalogue.db_store import <name>` imports keep working after the webui/cli/db/domain
reorg. The other submodules — `contributor_store`, `integrity`, `migrate_frbr` —
resolve as plain submodules (`from catalogue.db_store import integrity`).
"""
from .db import (
    InitGateError,
    SchemaDriftError,
    DryRunConnection,
    VOCAB_PATH,
    add_alias,
    assert_schema_current,
    connect,
    connect_ro,
    connect_rw,
    derive_holding_type,
    expected_schema,
    fold_key,
    init_db,
    load_vocab,
    new_export_db,
    nfc,
    schema_drift,
    schema_is_current,
    search_normalize,
    sqlite_source,
    _migrate,
)
from .paths import (
    DB_ENV,
    DATA_DIR_ENV,
    data_dir,
    default_db_path,
)
from . import authority_vocab
from .authority_vocab import vocab_config
from .store import Store, WriteError, as_store
from .external_contract import (
    CONTRACT_VERSION as EXTERNAL_READ_CONTRACT_VERSION,
    db_contract_version,
    descriptor as external_read_contract,
    verify as verify_external_read_contract,
)
from .reader_sync_contract import (
    CONTRACT_VERSION as READER_SYNC_CONTRACT_VERSION,
    api_version_payload as reader_sync_contract_version_payload,
    descriptor as reader_sync_contract_descriptor,
    verify as verify_reader_sync_contract,
)
