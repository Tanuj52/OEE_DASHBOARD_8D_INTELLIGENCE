-- ============================================================
-- 05_grants.sql
-- Grant permissions for the 8D system
-- ============================================================

USE SCHEMA MANUFACTURING_8D.PRODUCTION;

-- Grant usage to a specific role (adjust role name as needed)
-- GRANT USAGE ON DATABASE MANUFACTURING_8D TO ROLE <your_role>;
-- GRANT USAGE ON SCHEMA MANUFACTURING_8D.PRODUCTION TO ROLE <your_role>;
-- GRANT SELECT ON ALL TABLES IN SCHEMA MANUFACTURING_8D.PRODUCTION TO ROLE <your_role>;
-- GRANT SELECT ON ALL VIEWS IN SCHEMA MANUFACTURING_8D.PRODUCTION TO ROLE <your_role>;

-- Grant Cortex AI access (required for AI_COMPLETE)
-- GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE <your_role>;
