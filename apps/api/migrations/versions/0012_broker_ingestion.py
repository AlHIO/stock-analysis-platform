"""Frozen broker offline-import checkpoints and input-row audit ledgers."""
from alembic import op
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, Integer,
    MetaData, String, Table, Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import CreateIndex, CreateTable

revision = "0012_broker_ingestion"
down_revision = "0011_broker_flow"
branch_labels = None
depends_on = None


def _build_tables(metadata):
    jobs = Table("broker_flow_ingestion_jobs", metadata,
        Column("run_id", Uuid, primary_key=True), Column("source_id", Uuid, nullable=False),
        Column("selected_raw_id", Uuid), Column("phase", String(16), nullable=False),
        Column("next_group_index", Integer, nullable=False, server_default=text("0")),
        Column("group_count", Integer), Column("row_count", Integer),
        Column("config_hash", String(64), nullable=False),
        Column("plan_json", JSONB(none_as_null=True)), Column("plan_hash", String(64)),
        Column("failure_events", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["run_id", "source_id"],
            ["broker_flow_source_audits.run_id", "broker_flow_source_audits.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_job_audit"),
        ForeignKeyConstraint(["selected_raw_id", "run_id", "source_id"],
            ["broker_flow_raw_payloads.id", "broker_flow_raw_payloads.run_id", "broker_flow_raw_payloads.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_job_raw"),
        UniqueConstraint("run_id", "source_id", name="uq_broker_import_job_source"),
        CheckConstraint("phase IN ('started','archived','processing','interrupted','completed','failed')", name="phase_valid"),
        CheckConstraint("config_hash ~ '^[0-9a-f]{64}$'", name="config_hash_valid"),
        CheckConstraint("next_group_index>=0 AND (group_count IS NULL OR (group_count>=0 AND next_group_index<=group_count)) AND (row_count IS NULL OR row_count>=0)", name="cursor_valid"),
        CheckConstraint("(plan_json IS NULL AND plan_hash IS NULL AND group_count IS NULL AND row_count IS NULL AND next_group_index=0) OR (plan_json IS NOT NULL AND jsonb_typeof(plan_json)='object' AND plan_hash ~ '^[0-9a-f]{64}$' AND group_count IS NOT NULL AND row_count IS NOT NULL AND selected_raw_id IS NOT NULL)", name="plan_valid"),
        CheckConstraint("phase NOT IN ('archived','processing','interrupted','completed') OR selected_raw_id IS NOT NULL", name="archive_valid"),
        CheckConstraint("phase NOT IN ('processing','interrupted','completed') OR plan_json IS NOT NULL", name="processing_valid"),
        CheckConstraint("phase<>'completed' OR next_group_index=group_count", name="completion_valid"),
        CheckConstraint("updated_at>=created_at", name="timing_valid"),
        CheckConstraint("jsonb_typeof(failure_events)='array'", name="failure_history_valid"),
        Index("ix_broker_import_job_created", "created_at", "run_id"),
        Index("ix_broker_import_job_source_phase", "source_id", "phase"))
    groups = Table("broker_flow_import_groups", metadata,
        Column("run_id", Uuid, primary_key=True), Column("group_index", Integer, primary_key=True),
        Column("source_id", Uuid, nullable=False), Column("raw_id", Uuid, nullable=False),
        Column("report_id", String(256), nullable=False), Column("expected_row_count", Integer, nullable=False),
        Column("group_json", JSONB, nullable=False), Column("group_hash", String(64), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["run_id", "source_id"],
            ["broker_flow_ingestion_jobs.run_id", "broker_flow_ingestion_jobs.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_group_job"),
        ForeignKeyConstraint(["raw_id", "run_id", "source_id"],
            ["broker_flow_raw_payloads.id", "broker_flow_raw_payloads.run_id", "broker_flow_raw_payloads.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_group_raw"),
        CheckConstraint("group_index>=0 AND expected_row_count>=0", name="indices_valid"),
        CheckConstraint("report_id=btrim(report_id) AND report_id<>'' AND group_hash ~ '^[0-9a-f]{64}$'", name="identity_valid"),
        CheckConstraint("jsonb_typeof(group_json)='object' AND jsonb_typeof(group_json->'issues')='array' AND jsonb_array_length(group_json->'issues')=expected_row_count AND (group_json->>'index')::integer=group_index AND group_json->>'report_id'=report_id", name="snapshot_valid"))
    rows = Table("broker_flow_import_rows", metadata,
        Column("run_id", Uuid, primary_key=True), Column("row_index", Integer, primary_key=True),
        Column("source_id", Uuid, nullable=False), Column("raw_id", Uuid, nullable=False),
        Column("group_index", Integer, nullable=False), Column("row_locator", String(512), nullable=False),
        Column("report_observation_id", Uuid, nullable=False),
        Column("disposition", String(16), nullable=False), Column("reason", String(512), nullable=False),
        Column("raw_row", JSONB(none_as_null=True)),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["run_id", "source_id"],
            ["broker_flow_ingestion_jobs.run_id", "broker_flow_ingestion_jobs.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_row_job"),
        ForeignKeyConstraint(["run_id", "group_index"],
            ["broker_flow_import_groups.run_id", "broker_flow_import_groups.group_index"],
            ondelete="RESTRICT", name="fk_broker_import_row_group"),
        ForeignKeyConstraint(["raw_id", "run_id", "source_id"],
            ["broker_flow_raw_payloads.id", "broker_flow_raw_payloads.run_id", "broker_flow_raw_payloads.source_id"],
            ondelete="RESTRICT", name="fk_broker_import_row_raw"),
        ForeignKeyConstraint(["report_observation_id"], ["broker_flow_ingestion_items.id"],
            ondelete="RESTRICT", name="fk_broker_import_row_observation"),
        UniqueConstraint("run_id", "row_locator", name="uq_broker_import_row_locator"),
        CheckConstraint("row_index>=0 AND group_index>=0", name="indices_valid"),
        CheckConstraint("disposition IN ('accepted','partial','rejected','unresolved','skipped')", name="disposition_valid"),
        CheckConstraint("row_locator=btrim(row_locator) AND row_locator<>'' AND reason=btrim(reason) AND reason<>''", name="text_valid"),
        CheckConstraint("raw_row IS NULL OR jsonb_typeof(raw_row)='object'", name="raw_row_valid"),
        Index("ix_broker_import_row_group", "run_id", "group_index"),
        Index("ix_broker_import_row_disposition", "run_id", "disposition"),
        Index("ix_broker_import_row_observation", "report_observation_id"))
    return jobs, groups, rows


_GUARDS = r"""
CREATE FUNCTION broker_import_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE expected_count integer; actual_count integer; prior_event jsonb; event_index integer;
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'Broker import jobs cannot be deleted' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' THEN
  IF NEW.run_id<>OLD.run_id OR NEW.source_id<>OLD.source_id OR NEW.config_hash<>OLD.config_hash
   OR NEW.created_at<>OLD.created_at
   OR (OLD.selected_raw_id IS NOT NULL AND NEW.selected_raw_id IS DISTINCT FROM OLD.selected_raw_id)
   OR (OLD.plan_json IS NOT NULL AND (NEW.plan_json IS DISTINCT FROM OLD.plan_json
       OR NEW.plan_hash IS DISTINCT FROM OLD.plan_hash OR NEW.group_count IS DISTINCT FROM OLD.group_count
       OR NEW.row_count IS DISTINCT FROM OLD.row_count))
   OR NEW.next_group_index<OLD.next_group_index OR NEW.next_group_index>OLD.next_group_index+1
   OR (OLD.phase IN ('completed','failed') AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD))
   OR jsonb_array_length(NEW.failure_events)<jsonb_array_length(OLD.failure_events)
   OR jsonb_array_length(NEW.failure_events)>jsonb_array_length(OLD.failure_events)+1 THEN
   RAISE EXCEPTION 'Broker import identity, plan or cursor is immutable' USING ERRCODE='23514';
  END IF;
  FOR prior_event,event_index IN SELECT value,ordinality::integer-1 FROM jsonb_array_elements(OLD.failure_events) WITH ORDINALITY LOOP
   IF NEW.failure_events->event_index IS DISTINCT FROM prior_event THEN
    RAISE EXCEPTION 'Broker failure history cannot change' USING ERRCODE='23514';
   END IF;
  END LOOP;
  IF NEW.next_group_index=OLD.next_group_index+1 THEN
   IF OLD.phase<>'processing' OR NEW.phase<>'processing' THEN
    RAISE EXCEPTION 'Only an active broker import can advance' USING ERRCODE='23514';
   END IF;
   SELECT expected_row_count INTO expected_count FROM broker_flow_import_groups
    WHERE run_id=OLD.run_id AND group_index=OLD.next_group_index;
   SELECT count(*) INTO actual_count FROM broker_flow_import_rows
    WHERE run_id=OLD.run_id AND group_index=OLD.next_group_index;
   IF expected_count IS NULL OR expected_count<>actual_count
    OR NOT EXISTS(SELECT 1 FROM broker_flow_ingestion_items
      WHERE run_id=OLD.run_id AND raw_id=OLD.selected_raw_id AND row_index=OLD.next_group_index) THEN
    RAISE EXCEPTION 'Broker report and row audits must commit before cursor advance' USING ERRCODE='23514';
   END IF;
  END IF;
 END IF;
 IF NEW.plan_json IS NOT NULL AND (
   (NEW.plan_json->>'group_count')::integer IS DISTINCT FROM NEW.group_count
   OR NEW.plan_json->>'raw_id' IS DISTINCT FROM NEW.selected_raw_id::text
   OR NEW.plan_json->>'config_hash' IS DISTINCT FROM NEW.config_hash
   OR (NEW.plan_json->>'row_count')::integer IS DISTINCT FROM NEW.row_count) THEN
  RAISE EXCEPTION 'Broker processing plan does not match the selected archive' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION broker_import_row_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE job broker_flow_ingestion_jobs%ROWTYPE; observation broker_flow_ingestion_items%ROWTYPE;
BEGIN
 SELECT * INTO STRICT job FROM broker_flow_ingestion_jobs WHERE run_id=NEW.run_id;
 SELECT * INTO STRICT observation FROM broker_flow_ingestion_items WHERE id=NEW.report_observation_id;
 IF job.phase<>'processing' OR job.next_group_index<>NEW.group_index
  OR job.selected_raw_id<>NEW.raw_id OR job.source_id<>NEW.source_id
  OR observation.run_id<>NEW.run_id OR observation.raw_id<>NEW.raw_id
  OR observation.source_id<>NEW.source_id OR observation.row_index<>NEW.group_index
  OR NEW.row_index>=job.row_count THEN
  RAISE EXCEPTION 'Broker input row and report audit lineage disagree' USING ERRCODE='23514';
 END IF;
 IF observation.disposition='accepted' AND NEW.disposition<>'accepted' THEN
  RAISE EXCEPTION 'Complete broker reports require every input row to be accepted' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION broker_import_group_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE job broker_flow_ingestion_jobs%ROWTYPE;
BEGIN
 SELECT * INTO STRICT job FROM broker_flow_ingestion_jobs WHERE run_id=NEW.run_id;
 IF job.phase<>'processing' OR NEW.source_id<>job.source_id OR NEW.raw_id<>job.selected_raw_id
  OR NEW.group_index>=job.group_count THEN
  RAISE EXCEPTION 'Broker frozen group does not match its import plan' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION broker_import_plan_seal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE groups_count integer; rows_count bigint;
BEGIN
 SELECT count(*),COALESCE(sum(expected_row_count),0) INTO groups_count,rows_count
  FROM broker_flow_import_groups WHERE run_id=NEW.run_id;
 IF groups_count<>NEW.group_count OR rows_count<>NEW.row_count THEN
  RAISE EXCEPTION 'Broker import plan must contain every group atomically' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END; $$;
"""


def upgrade():
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })
    Table("broker_flow_source_audits", metadata, Column("run_id", Uuid), Column("source_id", Uuid))
    Table("broker_flow_raw_payloads", metadata, Column("id", Uuid), Column("run_id", Uuid), Column("source_id", Uuid))
    Table("broker_flow_ingestion_items", metadata, Column("id", Uuid))
    for table in _build_tables(metadata):
        op.execute(CreateTable(table))
        for index in sorted(table.indexes, key=lambda value: value.name):
            op.execute(CreateIndex(index))
    op.create_index("ix_broker_raw_content_first_seen", "broker_flow_raw_payloads",
                    ["source_id", "source_contract_version", "sha256", "fetched_at"])
    op.execute(text(_GUARDS))
    op.execute(text("CREATE TRIGGER broker_import_job_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON broker_flow_ingestion_jobs FOR EACH ROW EXECUTE FUNCTION broker_import_job_guard()"))
    op.execute(text("CREATE TRIGGER broker_import_row_guard_trigger BEFORE INSERT ON broker_flow_import_rows FOR EACH ROW EXECUTE FUNCTION broker_import_row_guard()"))
    op.execute(text("CREATE TRIGGER broker_import_rows_immutable BEFORE UPDATE OR DELETE ON broker_flow_import_rows FOR EACH ROW EXECUTE FUNCTION broker_flow_immutable()"))
    op.execute(text("CREATE TRIGGER broker_import_group_guard_trigger BEFORE INSERT ON broker_flow_import_groups FOR EACH ROW EXECUTE FUNCTION broker_import_group_guard()"))
    op.execute(text("CREATE TRIGGER broker_import_groups_immutable BEFORE UPDATE OR DELETE ON broker_flow_import_groups FOR EACH ROW EXECUTE FUNCTION broker_flow_immutable()"))
    op.execute(text("CREATE CONSTRAINT TRIGGER broker_import_plan_seal_insert AFTER INSERT ON broker_flow_ingestion_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.plan_json IS NOT NULL) EXECUTE FUNCTION broker_import_plan_seal()"))
    op.execute(text("CREATE CONSTRAINT TRIGGER broker_import_plan_seal_update AFTER UPDATE ON broker_flow_ingestion_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (OLD.plan_json IS NULL AND NEW.plan_json IS NOT NULL) EXECUTE FUNCTION broker_import_plan_seal()"))
    for name in ("broker_flow_ingestion_jobs", "broker_flow_import_groups", "broker_flow_import_rows"):
        op.execute(text(f"CREATE TRIGGER {name}_truncate_guard BEFORE TRUNCATE ON {name} FOR EACH STATEMENT EXECUTE FUNCTION broker_flow_immutable()"))


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("Offline downgrade cannot verify broker import checkpoints")
    connection = op.get_bind()
    connection.execute(text("LOCK TABLE broker_flow_import_rows, broker_flow_import_groups, broker_flow_ingestion_jobs IN ACCESS EXCLUSIVE MODE"))
    for name in ("broker_flow_import_rows", "broker_flow_import_groups", "broker_flow_ingestion_jobs"):
        if connection.scalar(text(f'SELECT EXISTS(SELECT 1 FROM "{name}")')):
            raise RuntimeError("Downgrade refused: broker import checkpoints or row audits exist")
    op.drop_index("ix_broker_raw_content_first_seen", table_name="broker_flow_raw_payloads")
    op.drop_table("broker_flow_import_rows")
    op.drop_table("broker_flow_import_groups")
    op.drop_table("broker_flow_ingestion_jobs")
    op.execute(text("DROP FUNCTION broker_import_row_guard()"))
    op.execute(text("DROP FUNCTION broker_import_job_guard()"))
    op.execute(text("DROP FUNCTION broker_import_group_guard()"))
    op.execute(text("DROP FUNCTION broker_import_plan_seal()"))

