-- ============================================================
-- 07_operations.sql
-- Live operations state written by the Streamlit app.
--
-- The app writes here only when something changes: a machine changes state
-- (running, tripped, in repair, commissioning), a work order moves, a recipient
-- list is edited, or an email is sent. Telemetry samples are not written.
-- The app creates these objects itself on first use when its role may; run this
-- script as an administrator when it may not.
-- ============================================================

CREATE SCHEMA IF NOT EXISTS MANUFACTURING_8D.OPERATIONS
  COMMENT = '8D Intelligence: live operations state written by the app';

-- Current state of every monitored machine (one row per machine)
CREATE TABLE IF NOT EXISTS MANUFACTURING_8D.OPERATIONS.MACHINE_STATE (
  MACHINE_ID STRING, MACHINE_NAME STRING, LINE STRING,
  LIFECYCLE STRING,        -- running | commissioning | tripped | stopped | in_repair
  STATUS STRING,           -- healthy | drift | warning | critical | learning | down
  CONDITION STRING,        -- wear pattern being tracked (healthy, wear, ...)
  FAULT_MODE STRING, FAULT_SENSOR STRING,
  HEALTH_PCT FLOAT, RUL_HOURS FLOAT, MODEL_VERSION NUMBER, WORK_ORDER_ID STRING,
  DOWN_SINCE TIMESTAMP_TZ, UPDATED_AT TIMESTAMP_TZ,
  DATA VARIANT, PRIMARY KEY (MACHINE_ID));

-- Maintenance work orders raised by the monitor (planned) or by a protective stop (unplanned)
CREATE TABLE IF NOT EXISTS MANUFACTURING_8D.OPERATIONS.WORK_ORDERS (
  WO_ID STRING, MACHINE_ID STRING, MACHINE_NAME STRING, LINE STRING,
  KIND STRING,             -- planned | unplanned
  PRIORITY STRING,         -- P1 emergency .. P3 routine
  STATUS STRING,           -- planned | awaiting | in_repair | commissioning | closed | cancelled
  FAILURE_MODE STRING, COMPONENT STRING, SENSOR STRING, REASON STRING,
  CREATED_AT TIMESTAMP_TZ, DOWN_AT TIMESTAMP_TZ, STARTED_AT TIMESTAMP_TZ, COMPLETED_AT TIMESTAMP_TZ, CLOSED_AT TIMESTAMP_TZ,
  RUL_HOURS_AT_OPEN FLOAT, EST_HOURS FLOAT, EST_COST_USD FLOAT, DOWNTIME_HOURS FLOAT,
  TECHNICIAN_ID STRING, TECHNICIAN_NAME STRING, NOTES STRING, RUN_ID STRING,
  DATA VARIANT, PRIMARY KEY (WO_ID));

-- Alerts and state changes from live monitoring (not every sample)
CREATE TABLE IF NOT EXISTS MANUFACTURING_8D.OPERATIONS.MACHINE_EVENTS (
  EVENT_ID STRING, EVENT_TS TIMESTAMP_TZ, MACHINE_ID STRING, LEVEL STRING, KIND STRING,
  MESSAGE STRING, DETAIL STRING, SESSION_ID STRING, DATA VARIANT);

-- Saved email distribution lists
CREATE TABLE IF NOT EXISTS MANUFACTURING_8D.OPERATIONS.RECIPIENT_LISTS (
  NAME STRING, EMAILS ARRAY, ON_REPORT BOOLEAN, ON_MACHINE_DOWN BOOLEAN, IS_DEFAULT BOOLEAN,
  UPDATED_AT TIMESTAMP_TZ, DATA VARIANT, PRIMARY KEY (NAME));

-- One row per email sent (or attempted)
CREATE TABLE IF NOT EXISTS MANUFACTURING_8D.OPERATIONS.EMAIL_LOG (
  EMAIL_ID STRING, SENT_AT TIMESTAMP_TZ, RUN_ID STRING, LIST_NAME STRING, RECIPIENTS ARRAY,
  SUBJECT STRING, TRANSPORT STRING, STATUS STRING, ERROR STRING, REASON STRING,
  DATA VARIANT, PRIMARY KEY (EMAIL_ID));

-- Finished 8D investigations; DATA holds the full report (steps, findings, decision)
CREATE TABLE IF NOT EXISTS MANUFACTURING_8D.OPERATIONS.INVESTIGATIONS (
  ID STRING, CREATED_AT TIMESTAMP_TZ, FINISHED_AT TIMESTAMP_TZ, STATUS STRING,
  MACHINE_ID STRING, SOURCE STRING, DECISION STRING, TOTAL_TOKENS NUMBER,
  DATA VARIANT, PRIMARY KEY (ID));

-- Plant-wide app settings shared by every viewer (email, automation, assistant,
-- pipeline). Secrets are never stored here: they come from Snowflake secrets.
CREATE TABLE IF NOT EXISTS MANUFACTURING_8D.OPERATIONS.APP_SETTINGS (
  NAME STRING, UPDATED_AT TIMESTAMP_TZ, UPDATED_BY STRING,
  DATA VARIANT, PRIMARY KEY (NAME));

-- Corrective actions, the spare-parts ledger and purchase orders are created by 10_business.sql
-- (with the stock views and the daily tasks).

-- Grants for the role that runs the deployed app
-- GRANT USAGE ON SCHEMA MANUFACTURING_8D.OPERATIONS TO ROLE <app_role>;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA MANUFACTURING_8D.OPERATIONS TO ROLE <app_role>;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA MANUFACTURING_8D.OPERATIONS TO ROLE <app_role>;
