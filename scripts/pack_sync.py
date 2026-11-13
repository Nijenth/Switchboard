import os
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from pack_validation import Pack, PackError
from psycopg.types.json import Jsonb

INITIAL_IDS = {
    "ecommerce_support": UUID("30000000-0000-0000-0000-000000000001"),
    "sales_leads": UUID("30000000-0000-0000-0000-000000000002"),
    "invoice_processing": UUID("30000000-0000-0000-0000-000000000003"),
}
EVAL_COLUMNS = (
    "case_key",
    "channel",
    "sender_address",
    "subject",
    "message",
    "form_fields",
    "document_fixture",
    "expected_intent",
    "expected_priority",
    "expected_decision",
    "expected_extraction",
    "expected_tools",
    "forbidden_tools",
    "tags",
    "notes",
)


def write_tool(conn: psycopg.Connection[Any], tool: dict[str, Any]) -> None:
    key = tool["key"]
    data = (
        tool["name"],
        tool["description"],
        tool["risk_level"],
        tool["requires_verified_identity"],
        tool.get("input_schema", {}),
        tool.get("output_schema", {}),
        tool["timeout_ms"],
    )
    existing = conn.execute(
        """SELECT name, description, risk_level, requires_verified_identity, input_schema,
                  output_schema, timeout_ms FROM public.tools WHERE key = %s""",
        (key,),
    ).fetchone()
    if existing is not None:
        if existing != data:
            raise PackError(f"tool {key}: released contract changed; use a new tool key")
        return
    conn.execute(
        """INSERT INTO public.tools
           (key,name,description,risk_level,requires_verified_identity,input_schema,output_schema,timeout_ms)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (key, *data[:4], Jsonb(data[4]), Jsonb(data[5]), data[6]),
    )


def publish(conn: psycopg.Connection[Any], pack: Pack) -> None:
    config = pack.config
    key, version = config["key"], config["version"]
    existing = conn.execute(
        "SELECT checksum FROM public.packs WHERE key=%s AND version=%s", (key, version)
    ).fetchone()
    if existing is not None:
        if existing != (pack.checksum,):
            raise PackError(f"{key} v{version}: released checksum changed; bump the version")
        print(f"Unchanged {key} v{version}")
        return
    identity = INITIAL_IDS.get(key) if version == 1 else None
    pack_id = identity or uuid5(NAMESPACE_URL, f"switchboard:pack:{key}:{version}")
    conn.execute(
        """INSERT INTO public.packs (id,key,version,name,description,checksum,definition)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (
            pack_id,
            key,
            version,
            config["name"],
            config["description"],
            pack.checksum,
            Jsonb({**config, "rules": pack.rules, "tools": pack.tools}),
        ),
    )
    for intent in config["intents"]:
        conn.execute(
            """INSERT INTO public.pack_intents (pack_id,intent_key,description,default_priority)
               VALUES (%s,%s,%s,%s)""",
            (pack_id, intent["key"], intent["description"], intent["default_priority"]),
        )
    for tool in pack.tools:
        approval = tool.get("approval", {})
        conn.execute(
            """INSERT INTO public.pack_tools (pack_id,tool_key,approval_role,approval_above_amount)
               VALUES (%s,%s,%s,%s)""",
            (pack_id, tool["key"], approval.get("role"), approval.get("above_amount")),
        )
    for case in pack.cases:
        values = {
            **case,
            "form_fields": Jsonb(case.get("form_fields", {})),
            "expected_extraction": Jsonb(case["expected_extraction"])
            if "expected_extraction" in case
            else None,
            **{
                column: case.get(column, [])
                for column in ("expected_tools", "forbidden_tools", "tags")
            },
        }
        placeholders = ",".join(["%s"] * (len(EVAL_COLUMNS) + 2))
        conn.execute(
            f"INSERT INTO public.eval_cases (pack_key,pack_version,{','.join(EVAL_COLUMNS)}) VALUES ({placeholders})",
            (key, version, *(values.get(column) for column in EVAL_COLUMNS)),
        )
    print(f"Loaded {key} v{version}")


def write_knowledge(conn: psycopg.Connection[Any], pack: Pack, tenant: UUID) -> None:
    config = pack.config
    if not conn.execute(
        """SELECT 1 FROM public.tenant_packs tp JOIN public.packs p ON p.id=tp.pack_id
           WHERE tp.tenant_id=%s AND tp.enabled AND p.key=%s AND p.version=%s""",
        (tenant, config["key"], config["version"]),
    ).fetchone():
        return
    for entry, content in pack.knowledge:
        checksum = sha256(content.encode()).hexdigest()
        existing = conn.execute(
            """SELECT id, checksum FROM public.knowledge_documents
               WHERE tenant_id=%s AND title=%s AND version=%s AND pack_key=%s""",
            (tenant, entry["title"], config["version"], config["key"]),
        ).fetchone()
        if existing:
            if existing[1] != checksum:
                raise PackError(f"{entry['file']}: published knowledge checksum changed")
            continue
        inserted = conn.execute(
            """INSERT INTO public.knowledge_documents
               (tenant_id,pack_key,title,doc_type,source_uri,version,checksum)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                tenant,
                config["key"],
                entry["title"],
                entry["doc_type"],
                entry["file"],
                config["version"],
                checksum,
            ),
        ).fetchone()
        assert inserted
        conn.execute(
            """INSERT INTO public.knowledge_chunks (tenant_id,document_id,pack_key,chunk_index,content)
               VALUES (%s,%s,%s,0,%s)""",
            (tenant, inserted[0], config["key"], content),
        )


def sync(registry: dict[str, dict[str, Any]], packs: list[Pack], tenant_id: str | None) -> None:
    host = os.environ.get("SWITCHBOARD_DB_SOCKET") or os.environ.get("SWITCHBOARD_DB_HOST")
    database = os.environ.get("SWITCHBOARD_MIGRATION_DATABASE") or os.environ.get(
        "SWITCHBOARD_DB_NAME"
    )
    password = os.environ.get("SWITCHBOARD_CONFIG_LOADER_PASSWORD")
    if not host or not database or not password:
        raise PackError("sync requires explicit database host, name, and config-loader password")
    try:
        with psycopg.connect(
            host=host,
            port=int(os.environ.get("SWITCHBOARD_DB_PORT", "5432")),
            dbname=database,
            user="switchboard_config_loader",
            password=password,
            connect_timeout=5,
        ) as conn:
            identity = conn.execute(
                """SELECT session_user, current_user,
                          pg_has_role(current_user,'app_config_loader','MEMBER'),
                          rolsuper OR rolbypassrls OR pg_has_role(current_user,'switchboard_owner','MEMBER')
                   FROM pg_catalog.pg_roles WHERE rolname=current_user"""
            ).fetchone()
            if identity != (
                "switchboard_config_loader",
                "switchboard_config_loader",
                True,
                False,
            ):
                raise PackError("sync requires the restricted config-loader login")
            conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(6397562149380320130)")
            for tool in registry.values():
                write_tool(conn, tool)
            for pack in packs:
                publish(conn, pack)
                if tenant_id is not None:
                    write_knowledge(conn, pack, UUID(tenant_id))
    except psycopg.Error as error:
        raise PackError(
            f"catalog transaction failed (SQLSTATE {error.sqlstate or 'connection'})"
        ) from None
