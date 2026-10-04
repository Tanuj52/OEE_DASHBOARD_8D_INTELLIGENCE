-- ============================================================
-- 08_external_email.sql
-- Email reports and machine-down alerts to ANY address (customers,
-- suppliers, people outside this Snowflake account), with the PDF attached.
--
-- Snowflake email (06_report_email.sql) only reaches verified users of this
-- account. For anyone else the deployed app sends through your own mail
-- service: an SMTP relay or the SendGrid API. Inside Snowflake that needs
--   1. a network rule for the mail host,
--   2. a secret with the credentials,
--   3. an external access integration that allows both,
--   4. the integration and secret attached to the Streamlit app.
-- Then pick the transport in the app: Settings > Email & recipients.
--
-- Run as an administrator. Replace the <...> placeholders. Keep only the
-- block for the transport you use.
-- ============================================================

USE ROLE ACCOUNTADMIN;
USE SCHEMA MANUFACTURING_8D.PRODUCTION;

-- ------------------------------------------------------------
-- Option A: SMTP relay (Office 365, Gmail, Amazon SES, Mailgun, company relay)
-- ------------------------------------------------------------
CREATE OR REPLACE NETWORK RULE EIGHT_D_SMTP_RULE
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = ('<smtp.office365.com>:587');      -- host:port, e.g. 'email-smtp.us-east-1.amazonaws.com:587'

-- Mapped into the app as 'mail_smtp' -> env mail_smtp_USERNAME / mail_smtp_PASSWORD
CREATE OR REPLACE SECRET EIGHT_D_SMTP_SECRET
  TYPE = PASSWORD
  USERNAME = '<smtp-username>'
  PASSWORD = '<smtp-password-or-app-password>';

-- ------------------------------------------------------------
-- Option B: SendGrid HTTPS API
-- ------------------------------------------------------------
CREATE OR REPLACE NETWORK RULE EIGHT_D_SENDGRID_RULE
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = ('api.sendgrid.com:443');

-- Mapped into the app as 'mail_sendgrid_key'
CREATE OR REPLACE SECRET EIGHT_D_SENDGRID_SECRET
  TYPE = GENERIC_STRING
  SECRET_STRING = '<SG.xxxxxxxx>';

-- ------------------------------------------------------------
-- External access integration (list the rules and secrets you created)
-- ------------------------------------------------------------
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION EIGHT_D_MAIL_EAI
  ALLOWED_NETWORK_RULES = (EIGHT_D_SMTP_RULE, EIGHT_D_SENDGRID_RULE)
  ALLOWED_AUTHENTICATION_SECRETS = (EIGHT_D_SMTP_SECRET, EIGHT_D_SENDGRID_SECRET)
  ENABLED = TRUE
  COMMENT = '8D Intelligence: outbound report and alert email';

-- Let the role that owns the app use them
-- GRANT USAGE ON INTEGRATION EIGHT_D_MAIL_EAI TO ROLE <app_owner_role>;
-- GRANT READ ON SECRET EIGHT_D_SMTP_SECRET TO ROLE <app_owner_role>;
-- GRANT READ ON SECRET EIGHT_D_SENDGRID_SECRET TO ROLE <app_owner_role>;

-- ------------------------------------------------------------
-- Attach to the deployed app (or uncomment the matching keys in snowflake.yml)
-- ------------------------------------------------------------
ALTER STREAMLIT MANUFACTURING_8D.PRODUCTION.EIGHT_D_INTELLIGENCE
  SET EXTERNAL_ACCESS_INTEGRATIONS = (EIGHT_D_MAIL_EAI)
      SECRETS = ('mail_smtp' = EIGHT_D_SMTP_SECRET, 'mail_sendgrid_key' = EIGHT_D_SENDGRID_SECRET);

ALTER STREAMLIT MANUFACTURING_8D.PRODUCTION.EIGHT_D_INTELLIGENCE ADD LIVE VERSION FROM LAST;
