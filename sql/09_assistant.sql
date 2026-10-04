-- ============================================================
-- 09_assistant.sql
-- Plant Assistant: the chat window in the bottom-right corner of the app.
--
--   PLANT_OPERATIONS_SV  semantic view over the plant tables (PRODUCTION) and the
--                        live operations tables the app writes (OPERATIONS)
--   PLANT_ASSISTANT      Cortex Agent that answers with Cortex Analyst over it
--
-- The app calls the agent through the Cortex Agents REST API
--   POST /api/v2/databases/MANUFACTURING_8D/schemas/OPERATIONS/agents/PLANT_ASSISTANT:run
-- with the container's session token inside Snowflake, or a programmatic access
-- token locally; it falls back to SNOWFLAKE.CORTEX.DATA_AGENT_RUN over SQL.
-- Requires 07_operations.sql (or one app session, which creates OPERATIONS).
-- ============================================================

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE COMPUTE_WH;

CREATE OR REPLACE SEMANTIC VIEW MANUFACTURING_8D.OPERATIONS.PLANT_OPERATIONS_SV
  TABLES (
    machines AS MANUFACTURING_8D.PRODUCTION.MACHINES PRIMARY KEY (machine_id)
      WITH SYNONYMS = ('assets', 'equipment') COMMENT = 'Plant machines: one row per machine with type, line and station',
    machine_state AS MANUFACTURING_8D.OPERATIONS.MACHINE_STATE PRIMARY KEY (machine_id)
      WITH SYNONYMS = ('live status', 'current state') COMMENT = 'Latest live-monitoring state per monitored machine (running, down, in repair), health and estimated time to failure',
    work_orders AS MANUFACTURING_8D.OPERATIONS.WORK_ORDERS PRIMARY KEY (wo_id)
      WITH SYNONYMS = ('maintenance jobs', 'tickets') COMMENT = 'Maintenance work orders raised by live monitoring: planned (before failure) or unplanned (after a protective stop)',
    machine_events AS MANUFACTURING_8D.OPERATIONS.MACHINE_EVENTS PRIMARY KEY (event_id)
      WITH SYNONYMS = ('alerts', 'alarms') COMMENT = 'Alerts and state changes from live monitoring',
    maintenance_logs AS MANUFACTURING_8D.PRODUCTION.MAINTENANCE_LOGS PRIMARY KEY (log_id)
      COMMENT = 'Historical maintenance jobs with duration and cost',
    production_orders AS MANUFACTURING_8D.PRODUCTION.PRODUCTION_ORDERS PRIMARY KEY (order_id)
      COMMENT = 'Production orders run on each machine',
    defects AS MANUFACTURING_8D.PRODUCTION.DEFECT_RECORDS PRIMARY KEY (defect_id)
      COMMENT = 'Quality defects found at inspection, linked to the production order',
    capa_actions AS MANUFACTURING_8D.OPERATIONS.CAPA_ACTIONS PRIMARY KEY (action_id)
      WITH SYNONYMS = ('corrective actions', 'CAPA', '8D actions', 'action items')
      COMMENT = 'Containment (D3), corrective (D6) and preventive (D7) actions from finished 8D reports, with owner, due date and status',
    part_stock AS MANUFACTURING_8D.OPERATIONS.V_PART_STOCK PRIMARY KEY (part_id)
      WITH SYNONYMS = ('spare parts', 'inventory', 'stock')
      COMMENT = 'Spare-part stock now: on hand, held for work orders, free, on order, reorder point, vendor and lead time',
    purchase_orders AS MANUFACTURING_8D.OPERATIONS.PURCHASE_ORDERS PRIMARY KEY (po_id)
      WITH SYNONYMS = ('purchase requests', 'POs')
      COMMENT = 'Spare-part purchase orders: requested, approved, ordered, received or cancelled',
    order_book AS MANUFACTURING_8D.PRODUCTION.ORDER_BOOK PRIMARY KEY (order_id)
      WITH SYNONYMS = ('customer orders', 'sales orders', 'backlog')
      COMMENT = 'Open and recently shipped customer orders per machine with value, due date, priority and late penalty',
    customers AS MANUFACTURING_8D.PRODUCTION.CUSTOMERS PRIMARY KEY (customer_id)
      COMMENT = 'Customers who place the orders'
  )
  RELATIONSHIPS (
    state_machine AS machine_state (machine_id) REFERENCES machines,
    wo_machine AS work_orders (machine_id) REFERENCES machines,
    event_machine AS machine_events (machine_id) REFERENCES machines,
    log_machine AS maintenance_logs (machine_id) REFERENCES machines,
    order_machine AS production_orders (machine_id) REFERENCES machines,
    defect_order AS defects (order_id) REFERENCES production_orders,
    capa_machine AS capa_actions (machine_id) REFERENCES machines,
    book_machine AS order_book (machine_id) REFERENCES machines,
    book_customer AS order_book (customer_id) REFERENCES customers,
    po_part AS purchase_orders (part_id) REFERENCES part_stock
  )
  FACTS (
    machine_state.health_pct AS health_pct COMMENT = 'Health 0-100: distance of the worst signal from its critical limit',
    machine_state.rul_hours AS rul_hours COMMENT = 'Estimated hours until a signal reaches its critical limit (time to failure); null when no wear trend',
    work_orders.est_hours AS est_hours COMMENT = 'Estimated repair duration in hours',
    work_orders.est_cost_usd AS est_cost_usd COMMENT = 'Estimated repair cost in USD',
    work_orders.downtime_hours AS downtime_hours COMMENT = 'Hours the machine was down for this work order (closed orders)',
    work_orders.rul_hours_at_open AS rul_hours_at_open COMMENT = 'Forecast hours to failure when the order was raised (0 for breakdowns)',
    maintenance_logs.duration_hrs AS duration_hrs,
    maintenance_logs.cost_usd AS cost_usd,
    production_orders.qty_produced AS qty_produced,
    order_book.order_qty AS qty COMMENT = 'Units ordered',
    order_book.order_value_usd AS order_value_usd COMMENT = 'Order value in USD (quantity x unit price)',
    order_book.late_penalty_usd_per_day AS late_penalty_usd_per_day COMMENT = 'Penalty per day the order ships after its due date',
    part_stock.on_hand_qty AS on_hand COMMENT = 'Units on the shelf',
    part_stock.reserved_qty AS reserved COMMENT = 'Units held for open work orders',
    part_stock.available_qty AS available COMMENT = 'Units free to use (on hand minus held; negative means short)',
    part_stock.incoming_qty AS incoming COMMENT = 'Units on open purchase orders',
    part_stock.reorder_point AS reorder_point,
    part_stock.stock_value_usd AS stock_value_usd,
    part_stock.lead_time_days AS lead_time_days COMMENT = 'Vendor lead time in days',
    purchase_orders.po_qty AS qty,
    purchase_orders.po_unit_cost_usd AS unit_cost_usd
  )
  DIMENSIONS (
    machines.machine_id AS machine_id WITH SYNONYMS = ('machine', 'asset id'),
    machines.machine_name AS name,
    machines.machine_type AS type COMMENT = 'CNC, Injection, Press, Assembly, Welding, ...',
    machines.line AS line WITH SYNONYMS = ('production line'),
    machines.critical_components AS critical_components,
    machine_state.lifecycle AS lifecycle COMMENT = 'running, commissioning (re-learning after repair), tripped (stopped automatically), stopped (taken offline), in_repair',
    machine_state.live_status AS status COMMENT = 'healthy, drift, warning, critical, learning, down',
    machine_state.current_work_order AS work_order_id,
    machine_state.down_since AS down_since,
    machine_state.state_updated_at AS updated_at,
    work_orders.wo_id AS wo_id WITH SYNONYMS = ('work order', 'job number'),
    work_orders.wo_kind AS kind COMMENT = 'planned (raised before failure) or unplanned (after a breakdown)',
    work_orders.wo_priority AS priority COMMENT = 'P1 emergency, P2 urgent, P3 planned',
    work_orders.wo_status AS status COMMENT = 'planned, awaiting, in_repair, commissioning, closed, cancelled',
    work_orders.component AS component,
    work_orders.failure_mode AS failure_mode,
    work_orders.wo_reason AS reason,
    work_orders.technician_name AS technician_name,
    work_orders.created_at AS created_at,
    work_orders.closed_at AS closed_at,
    machine_events.event_ts AS event_ts,
    machine_events.event_level AS level COMMENT = 'info, success, warning, critical',
    machine_events.event_kind AS kind COMMENT = 'trip, degradation, anomaly, drift, retrain, rul_plan, rul_urgent, work_order, repair_started, repair_completed, recommissioned',
    machine_events.event_message AS message,
    maintenance_logs.maintenance_type AS type COMMENT = 'Scheduled or Unscheduled',
    maintenance_logs.maintenance_date AS date,
    maintenance_logs.component_replaced AS component_replaced,
    maintenance_logs.maintenance_description AS description,
    production_orders.order_start_date AS start_date,
    defects.defect_type AS defect_type,
    defects.severity AS severity,
    defects.containment_status AS containment_status,
    capa_actions.action_id AS action_id,
    capa_actions.action_text AS action COMMENT = 'What has to be done',
    capa_actions.discipline AS discipline COMMENT = 'D3 containment, D6 corrective, D7 preventive',
    capa_actions.action_status AS status COMMENT = 'open, in_progress, done, verified (effective), not_effective, cancelled',
    capa_actions.owner_name AS owner_name WITH SYNONYMS = ('owner', 'assignee', 'responsible'),
    capa_actions.owner_role AS owner_role,
    capa_actions.due_at AS due_at COMMENT = 'When the action is due',
    capa_actions.report_id AS run_id WITH SYNONYMS = ('8D report', 'investigation'),
    order_book.order_id AS order_id,
    order_book.order_status AS status COMMENT = 'Open (not started), In production, Shipped',
    order_book.order_priority AS priority COMMENT = 'Standard, Rush, Critical',
    order_book.order_due_date AS due_date,
    order_book.order_start_date AS start_date,
    order_book.product_id AS product_id,
    customers.customer_name AS name,
    customers.industry AS industry,
    part_stock.part_id AS part_id,
    part_stock.part_name AS part_name WITH SYNONYMS = ('part', 'spare'),
    part_stock.part_category AS category,
    part_stock.vendor_name AS vendor_name WITH SYNONYMS = ('supplier', 'vendor'),
    part_stock.to_order AS to_order COMMENT = 'TRUE when free stock plus open orders is at or below the reorder point',
    purchase_orders.po_id AS po_id,
    purchase_orders.po_status AS status COMMENT = 'requested, approved, ordered, received, cancelled',
    purchase_orders.expected_at AS expected_at
  )
  METRICS (
    machine_state.machines_down AS COUNT_IF(machine_state.lifecycle IN ('tripped', 'stopped', 'in_repair')) COMMENT = 'Machines currently not producing',
    machine_state.machines_monitored AS COUNT(machine_state.machine_id),
    machine_state.avg_health_pct AS AVG(machine_state.health_pct),
    machine_state.min_rul_hours AS MIN(machine_state.rul_hours) COMMENT = 'Shortest time to failure across machines',
    work_orders.work_order_count AS COUNT(work_orders.wo_id),
    work_orders.open_work_orders AS COUNT_IF(work_orders.wo_status IN ('planned', 'awaiting', 'in_repair', 'commissioning')),
    work_orders.planned_work_orders AS COUNT_IF(work_orders.wo_kind = 'planned' AND work_orders.wo_status <> 'cancelled') COMMENT = 'Failures caught early',
    work_orders.unplanned_work_orders AS COUNT_IF(work_orders.wo_kind = 'unplanned') COMMENT = 'Breakdowns',
    work_orders.total_downtime_hours AS SUM(work_orders.downtime_hours),
    work_orders.total_est_cost_usd AS SUM(work_orders.est_cost_usd),
    machine_events.alert_count AS COUNT_IF(machine_events.event_level IN ('warning', 'critical')),
    machine_events.trip_count AS COUNT_IF(machine_events.event_kind = 'trip') COMMENT = 'Protective stops',
    maintenance_logs.maintenance_jobs AS COUNT(maintenance_logs.duration_hrs),
    maintenance_logs.maintenance_cost_usd AS SUM(maintenance_logs.cost_usd),
    maintenance_logs.unscheduled_jobs AS COUNT_IF(maintenance_logs.maintenance_type = 'Unscheduled'),
    maintenance_logs.avg_repair_hours AS AVG(maintenance_logs.duration_hrs),
    defects.defect_count AS COUNT(defects.defect_type),
    capa_actions.open_actions AS COUNT_IF(capa_actions.action_status IN ('open', 'in_progress')),
    capa_actions.overdue_actions AS COUNT_IF(capa_actions.action_status IN ('open', 'in_progress') AND capa_actions.due_at < CURRENT_TIMESTAMP())
      COMMENT = 'Open actions past their due date',
    capa_actions.verified_actions AS COUNT_IF(capa_actions.action_status = 'verified'),
    capa_actions.not_effective_actions AS COUNT_IF(capa_actions.action_status = 'not_effective'),
    order_book.open_orders AS COUNT_IF(order_book.order_status <> 'Shipped'),
    order_book.open_order_value_usd AS SUM(IFF(order_book.order_status <> 'Shipped', order_book.order_value_usd, 0)),
    part_stock.parts_to_order AS COUNT_IF(part_stock.to_order),
    part_stock.total_stock_value_usd AS SUM(part_stock.stock_value_usd),
    purchase_orders.open_purchase_orders AS COUNT_IF(purchase_orders.po_status IN ('requested', 'approved', 'ordered')),
    purchase_orders.purchase_value_usd AS SUM(purchase_orders.po_qty * purchase_orders.po_unit_cost_usd)
  )
  COMMENT = 'Plant operations for the 8D Intelligence Plant Assistant: live machine state, work orders, alerts, maintenance history, defects, corrective actions, spare parts, purchasing and the customer order book';

CREATE OR REPLACE AGENT MANUFACTURING_8D.OPERATIONS.PLANT_ASSISTANT
  COMMENT = '8D Intelligence Plant Assistant: live machine state, work orders, maintenance and defects'
  PROFILE = '{"display_name": "Plant Assistant", "color": "orange"}'
  FROM SPECIFICATION
$$
models:
  orchestration: auto
orchestration:
  budget:
    seconds: 150
    tokens: 32000
instructions:
  system: >-
    You are the Plant Assistant in 8D Intelligence, a manufacturing quality and maintenance app. You help plant
    managers, maintenance planners and quality engineers understand which machines need attention, what is down,
    which work orders are open, what maintenance has cost, and how defects relate to machines. The app monitors
    machines in real time: it learns each machine's normal pattern, warns early, forecasts time to failure (remaining
    useful life, RUL), stops a machine automatically when a signal stays above its critical limit, and raises
    maintenance work orders.
  response: >-
    Answer in plain business language first, then give the key numbers. Keep answers short: two to five sentences or a
    small table. Express time to failure in minutes, hours or days, and money in USD. Explain technical terms the first
    time you use them (for example "time to failure (RUL)"). When something needs action, end with one concrete next
    step (schedule maintenance, start the repair, launch an 8D investigation). Do not invent numbers: if the data does
    not answer the question, say so.
  orchestration: >-
    Use the plant_operations tool for any question about machines, live state, health, time to failure, work orders,
    alerts, protective stops, maintenance history and cost, spare parts stock and purchase orders, corrective actions
    from 8D reports (owners, due dates, overdue, effectiveness), customer orders in the order book, or defects. The user's message may begin
    with a block marked [Live context] that the app adds with the current live view (session, machines down, machines
    at risk, the machine the user is looking at). Treat it as the latest real-time state, which can be newer than the
    tables; use it directly for questions about right now and use the tool for history, totals and comparisons.
  sample_questions:
    - question: Which machines are down right now and why?
    - question: Which corrective actions are overdue and who owns them?
    - question: Which spare parts do we need to order?
    - question: What customer orders are due on machines that are down?
    - question: How many failures did we catch early versus breakdowns, and what did breakdowns cost?
tools:
  - tool_spec:
      type: cortex_analyst_text_to_sql
      name: plant_operations
      description: >-
        Plant operations data: machines (type, line), live machine state (lifecycle running/down/in repair, status,
        health %, estimated hours to failure), maintenance work orders raised by monitoring (planned vs unplanned,
        priority, status, component, estimated cost and hours, downtime), live alerts and protective stops, historical
        maintenance logs (scheduled/unscheduled, duration, cost), quality defects by production order, corrective and
        preventive actions from 8D reports (owner, due date, status), spare-part stock (on hand, held, free, on order,
        reorder point, vendor, lead time), spare-part purchase orders, and the customer order book (open orders per
        machine with customer, value, due date, priority and late penalty).
tool_resources:
  plant_operations:
    execution_environment:
      type: warehouse
      warehouse: COMPUTE_WH
    semantic_view: MANUFACTURING_8D.OPERATIONS.PLANT_OPERATIONS_SV
$$;

-- Access for the role that owns or uses the app
-- GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE <app_role>;
-- GRANT SELECT ON SEMANTIC VIEW MANUFACTURING_8D.OPERATIONS.PLANT_OPERATIONS_SV TO ROLE <app_role>;
-- GRANT USAGE ON AGENT MANUFACTURING_8D.OPERATIONS.PLANT_ASSISTANT TO ROLE <app_role>;

-- Try it
-- SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN('MANUFACTURING_8D.OPERATIONS.PLANT_ASSISTANT',
--   '{"messages":[{"role":"user","content":[{"type":"text","text":"Which machines are down right now?"}]}],"stream":false}');
