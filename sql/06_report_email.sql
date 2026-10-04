-- ============================================================
-- 06_report_email.sql
-- Email delivery for 8D reports (Reports > Email report)
--
-- Snowflake email cannot carry attachments, so the app uploads the
-- report PDF to REPORT_STAGE (server-side encrypted, required for
-- pre-signed URLs) and emails an HTML summary with a download link.
-- Recipients must be users of this account with verified emails.
-- Run as a role with CREATE INTEGRATION (e.g. ACCOUNTADMIN).
-- ============================================================

USE SCHEMA MANUFACTURING_8D.PRODUCTION;

CREATE NOTIFICATION INTEGRATION IF NOT EXISTS EIGHT_D_REPORT_EMAIL
  TYPE = EMAIL
  ENABLED = TRUE
  COMMENT = '8D Intelligence: email 8D reports';

CREATE STAGE IF NOT EXISTS REPORT_STAGE
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  DIRECTORY = (ENABLE = TRUE)
  COMMENT = '8D Intelligence: emailed report PDFs';

-- Role that owns the Streamlit app (or runs it locally):
-- GRANT USAGE ON INTEGRATION EIGHT_D_REPORT_EMAIL TO ROLE <your_role>;
-- GRANT READ, WRITE ON STAGE REPORT_STAGE TO ROLE <your_role>;

-- Verify a recipient's email (they can also do this in Snowsight > Settings > Profile):
-- SELECT SYSTEM$START_USER_EMAIL_VERIFICATION('<user_name>');

-- Clean-up: PDFs are stored under <report_id>/<timestamp>/. Remove one report's copies with
-- REMOVE @REPORT_STAGE/<report_id>/;
