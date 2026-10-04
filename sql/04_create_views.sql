-- ============================================================
-- 04_create_views.sql
-- Analytical views for the 8D system
-- ============================================================

USE SCHEMA MANUFACTURING_8D.PRODUCTION;

-- Machine health summary (latest sensor readings + rolling averages)
CREATE OR REPLACE VIEW v_machine_health AS
SELECT
    s.machine_id,
    m.name AS machine_name,
    m.type AS machine_type,
    m.line,
    MAX(s.timestamp) AS last_reading,
    AVG(s.vibration_mm_s) AS avg_vibration,
    MAX(s.vibration_mm_s) AS max_vibration,
    AVG(s.temp_c) AS avg_temp,
    MAX(s.temp_c) AS max_temp,
    AVG(s.pressure_bar) AS avg_pressure,
    AVG(s.power_kw) AS avg_power,
    COUNT(*) AS reading_count
FROM sensor_readings s
JOIN machines m ON s.machine_id = m.machine_id
WHERE s.timestamp >= DATEADD(day, -7, CURRENT_TIMESTAMP())
GROUP BY s.machine_id, m.name, m.type, m.line;

-- Defect rate by machine
CREATE OR REPLACE VIEW v_defect_rate_by_machine AS
SELECT
    po.machine_id,
    m.name AS machine_name,
    COUNT(DISTINCT po.order_id) AS total_orders,
    COUNT(DISTINCT dr.defect_id) AS total_defects,
    SUM(po.qty_produced) AS total_produced,
    ROUND(COUNT(DISTINCT dr.defect_id) * 1000000.0 / NULLIF(SUM(po.qty_produced), 0), 0) AS defect_ppm
FROM production_orders po
JOIN machines m ON po.machine_id = m.machine_id
LEFT JOIN defect_records dr ON po.order_id = dr.order_id
GROUP BY po.machine_id, m.name;

-- Lot traceability: defect to supplier chain
CREATE OR REPLACE VIEW v_lot_traceability AS
SELECT
    dr.defect_id,
    dr.defect_type,
    dr.severity,
    qi.order_id,
    po.machine_id,
    po.product_id,
    po.start_date AS production_date,
    bc.material_lot_id,
    ml.supplier_id,
    sup.name AS supplier_name,
    ml.material,
    ml.received_date AS material_received,
    sh.shipment_id,
    sh.customer_id,
    c.name AS customer_name,
    sh.ship_date,
    sh.lot_number AS shipped_lot
FROM defect_records dr
JOIN quality_inspections qi ON dr.inspection_id = qi.inspection_id
JOIN production_orders po ON dr.order_id = po.order_id
LEFT JOIN bom_consumption bc ON po.order_id = bc.order_id
LEFT JOIN material_lots ml ON bc.material_lot_id = ml.lot_id
LEFT JOIN suppliers sup ON ml.supplier_id = sup.supplier_id
LEFT JOIN shipments sh ON po.order_id = sh.order_id
LEFT JOIN customers c ON sh.customer_id = c.customer_id;

-- Quality inspection summary with SPC-relevant stats
CREATE OR REPLACE VIEW v_inspection_stats AS
SELECT
    po.machine_id,
    m.name AS machine_name,
    qi.characteristic,
    COUNT(*) AS sample_count,
    AVG(qi.measured_value) AS mean_value,
    STDDEV(qi.measured_value) AS std_value,
    MIN(qi.measured_value) AS min_value,
    MAX(qi.measured_value) AS max_value,
    AVG(qi.usl) AS usl,
    AVG(qi.lsl) AS lsl,
    AVG(qi.nominal) AS nominal,
    SUM(CASE WHEN qi.pass_fail = 'FAIL' THEN 1 ELSE 0 END) AS fail_count,
    ROUND(SUM(CASE WHEN qi.pass_fail = 'FAIL' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS fail_rate_pct
FROM quality_inspections qi
JOIN production_orders po ON qi.order_id = po.order_id
JOIN machines m ON po.machine_id = m.machine_id
GROUP BY po.machine_id, m.name, qi.characteristic;

-- Night shift quality comparison (for Scenario C detection)
CREATE OR REPLACE VIEW v_shift_quality AS
SELECT
    pr.shift,
    pr.machine_id,
    m.name AS machine_name,
    m.line,
    COUNT(DISTINCT pr.order_id) AS order_count,
    AVG(pr.temperature_c) AS avg_temperature,
    COUNT(DISTINCT dr.defect_id) AS defect_count,
    ROUND(COUNT(DISTINCT dr.defect_id) * 100.0 / NULLIF(COUNT(DISTINCT pr.order_id), 0), 2) AS defect_rate_pct
FROM process_records pr
JOIN machines m ON pr.machine_id = m.machine_id
LEFT JOIN defect_records dr ON pr.order_id = dr.order_id
GROUP BY pr.shift, pr.machine_id, m.name, m.line;
