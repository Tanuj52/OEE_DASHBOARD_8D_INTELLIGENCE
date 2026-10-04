-- ============================================================
-- 02_create_tables.sql
-- Create the 16 plant tables for the 8D Manufacturing system
-- (the rolling ORDER_BOOK is created by 10_business.sql)
-- ============================================================

USE SCHEMA MANUFACTURING_8D.PRODUCTION;

CREATE OR REPLACE TABLE machines (
    machine_id       VARCHAR(10) PRIMARY KEY,
    name             VARCHAR(50),
    type             VARCHAR(20),
    line             VARCHAR(10),
    station          VARCHAR(15),
    install_date     DATE,
    critical_components VARCHAR(200),
    status           VARCHAR(15)
);

CREATE OR REPLACE TABLE operators (
    operator_id      VARCHAR(10) PRIMARY KEY,
    name             VARCHAR(50),
    shift            VARCHAR(15),
    certifications   VARCHAR(200),
    hire_date        DATE,
    training_status  VARCHAR(15)
);

CREATE OR REPLACE TABLE suppliers (
    supplier_id      VARCHAR(10) PRIMARY KEY,
    name             VARCHAR(50),
    material_type    VARCHAR(50),
    certification_level VARCHAR(100),
    rating           FLOAT,
    lead_time_days   INT
);

CREATE OR REPLACE TABLE customers (
    customer_id      VARCHAR(10) PRIMARY KEY,
    name             VARCHAR(50),
    industry         VARCHAR(30),
    plant_location   VARCHAR(50),
    quality_requirements VARCHAR(200)
);

CREATE OR REPLACE TABLE material_lots (
    lot_id           VARCHAR(15) PRIMARY KEY,
    supplier_id      VARCHAR(10) REFERENCES suppliers(supplier_id),
    material         VARCHAR(50),
    received_date    DATE,
    coa_status       VARCHAR(10),
    qty              INT,
    spec_grade       VARCHAR(15)
);

CREATE OR REPLACE TABLE production_orders (
    order_id         VARCHAR(15) PRIMARY KEY,
    product_id       VARCHAR(30),
    machine_id       VARCHAR(10) REFERENCES machines(machine_id),
    start_date       DATE,
    qty_planned      INT,
    qty_produced     INT,
    priority         VARCHAR(15),
    due_date         DATE,
    status           VARCHAR(15)
);

CREATE OR REPLACE TABLE bom_consumption (
    bom_id           VARCHAR(15) PRIMARY KEY,
    order_id         VARCHAR(15) REFERENCES production_orders(order_id),
    material_lot_id  VARCHAR(15) REFERENCES material_lots(lot_id),
    qty_consumed     INT,
    station          VARCHAR(15)
);

CREATE OR REPLACE TABLE sensor_readings (
    reading_id       VARCHAR(15) PRIMARY KEY,
    machine_id       VARCHAR(10) REFERENCES machines(machine_id),
    timestamp        TIMESTAMP,
    vibration_mm_s   FLOAT,
    temp_c           FLOAT,
    pressure_bar     FLOAT,
    power_kw         FLOAT,
    spindle_rpm      FLOAT,
    coolant_flow_lpm FLOAT
);

CREATE OR REPLACE TABLE process_records (
    record_id        VARCHAR(15) PRIMARY KEY,
    order_id         VARCHAR(15) REFERENCES production_orders(order_id),
    machine_id       VARCHAR(10) REFERENCES machines(machine_id),
    operator_id      VARCHAR(10) REFERENCES operators(operator_id),
    shift            VARCHAR(15),
    temperature_c    FLOAT,
    pressure_bar     FLOAT,
    speed_rpm        FLOAT,
    torque_nm        FLOAT,
    timestamp        DATE
);

CREATE OR REPLACE TABLE quality_inspections (
    inspection_id    VARCHAR(15) PRIMARY KEY,
    order_id         VARCHAR(15) REFERENCES production_orders(order_id),
    inspector_id     VARCHAR(10),
    characteristic   VARCHAR(30),
    measured_value   FLOAT,
    usl              FLOAT,
    lsl              FLOAT,
    nominal          FLOAT,
    pass_fail        VARCHAR(5),
    timestamp        DATE
);

CREATE OR REPLACE TABLE defect_records (
    defect_id        VARCHAR(15) PRIMARY KEY,
    inspection_id    VARCHAR(15) REFERENCES quality_inspections(inspection_id),
    order_id         VARCHAR(15) REFERENCES production_orders(order_id),
    defect_type      VARCHAR(30),
    severity         VARCHAR(15),
    description      VARCHAR(500),
    containment_status VARCHAR(15)
);

CREATE OR REPLACE TABLE maintenance_logs (
    log_id           VARCHAR(15) PRIMARY KEY,
    machine_id       VARCHAR(10) REFERENCES machines(machine_id),
    type             VARCHAR(15),
    description      VARCHAR(200),
    component_replaced VARCHAR(50),
    duration_hrs     FLOAT,
    cost_usd         FLOAT,
    technician_id    VARCHAR(10),
    date             DATE
);

CREATE OR REPLACE TABLE shipments (
    shipment_id      VARCHAR(15) PRIMARY KEY,
    order_id         VARCHAR(15) REFERENCES production_orders(order_id),
    customer_id      VARCHAR(10) REFERENCES customers(customer_id),
    ship_date        DATE,
    qty              INT,
    lot_number       VARCHAR(20),
    destination      VARCHAR(50),
    tracking_status  VARCHAR(15)
);

CREATE OR REPLACE TABLE parts_inventory (
    part_id          VARCHAR(15) PRIMARY KEY,
    part_name        VARCHAR(50),
    category         VARCHAR(20),
    machine_compatibility VARCHAR(200),
    qty_on_hand      INT,
    reorder_point    INT,
    unit_cost_usd    FLOAT
);

-- Products and their selling price (used to value orders and downtime)
CREATE OR REPLACE TABLE products (
    product_id       VARCHAR(20) PRIMARY KEY,
    product_name     VARCHAR(50),
    unit_price_usd   FLOAT
);

-- Who supplies each spare part, lead time and minimum order quantity
CREATE OR REPLACE TABLE part_vendors (
    part_id          VARCHAR(15) PRIMARY KEY,
    vendor_name      VARCHAR(50),
    lead_time_days   INT,
    min_order_qty    INT,
    unit_cost_usd    FLOAT,
    vendor_email     VARCHAR(80)
);
