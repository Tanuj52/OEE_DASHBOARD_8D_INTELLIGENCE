-- ============================================================
-- 03_load_data.sql
-- Load CSV data from stage into tables
-- Run after uploading CSVs to @MANUFACTURING_8D.PRODUCTION.DATA_STAGE
-- ============================================================

USE SCHEMA MANUFACTURING_8D.PRODUCTION;

-- Create file format for CSV files
CREATE OR REPLACE FILE FORMAT csv_format
    TYPE = 'CSV'
    FIELD_OPTIONALLY_ENCLOSED_BY = '"'
    SKIP_HEADER = 1
    NULL_IF = ('', 'NULL', 'None');

-- Create internal stage
CREATE OR REPLACE STAGE data_stage
    FILE_FORMAT = csv_format;

-- After running: PUT file:///path/to/synthetic/*.csv @data_stage/
-- Load each table

COPY INTO machines FROM @data_stage/machines.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO operators FROM @data_stage/operators.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO suppliers FROM @data_stage/suppliers.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO customers FROM @data_stage/customers.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO material_lots FROM @data_stage/material_lots.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO production_orders FROM @data_stage/production_orders.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO bom_consumption FROM @data_stage/bom_consumption.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO sensor_readings FROM @data_stage/sensor_readings.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO process_records FROM @data_stage/process_records.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO quality_inspections FROM @data_stage/quality_inspections.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO defect_records FROM @data_stage/defect_records.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO maintenance_logs FROM @data_stage/maintenance_logs.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO shipments FROM @data_stage/shipments.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
COPY INTO parts_inventory FROM @data_stage/parts_inventory.csv FILE_FORMAT = csv_format ON_ERROR = 'CONTINUE';
