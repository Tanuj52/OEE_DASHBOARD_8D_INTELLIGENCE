"""
Synthetic manufacturing data generator for the 8D Problem Solving system.

Creates 13 interlinked tables with 3 embedded defect scenarios:
  A: Bearing degradation on CNC Lathe MCH-005 (predictive signal in sensor data)
  B: Contaminated steel from Supplier SUP-004 (material lot traceability)
  C: Night shift process drift on Assembly Line-D (training gap)

Also includes a LiveSimulator for real-time demo monitoring.
"""

import os
import random
import hashlib
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SYNTHETIC_DIR = os.path.join(BASE_DIR, "synthetic")
os.makedirs(SYNTHETIC_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Static reference data
# ---------------------------------------------------------------------------

MACHINES_DATA = [
    ("MCH-001", "CNC Mill Alpha",      "CNC",       "Line-A", "Station-1", "2019-03-15", "spindle,bearings,coolant_pump", "Active"),
    ("MCH-002", "CNC Mill Beta",       "CNC",       "Line-A", "Station-2", "2019-06-20", "spindle,bearings,coolant_pump", "Active"),
    ("MCH-003", "CNC Lathe Gamma",     "CNC",       "Line-A", "Station-3", "2020-01-10", "chuck,bearings,turret",         "Active"),
    ("MCH-004", "CNC Lathe Delta",     "CNC",       "Line-B", "Station-1", "2020-04-05", "chuck,bearings,turret",         "Active"),
    ("MCH-005", "CNC Lathe Epsilon",   "CNC",       "Line-B", "Station-2", "2019-11-22", "chuck,bearings,turret",         "Active"),
    ("MCH-006", "Injection Molder 1",  "Injection",  "Line-C", "Station-1", "2018-07-01", "barrel,screw,heater_bands",    "Active"),
    ("MCH-007", "Injection Molder 2",  "Injection",  "Line-C", "Station-2", "2021-02-14", "barrel,screw,heater_bands",    "Active"),
    ("MCH-008", "Laser Cutter X1",     "Laser",      "Line-C", "Station-3", "2022-05-30", "laser_tube,lens,mirrors",      "Active"),
    ("MCH-009", "Assembly Robot R1",   "Assembly",   "Line-D", "Station-1", "2020-09-18", "servo_motors,grippers,vision",  "Active"),
    ("MCH-010", "Assembly Robot R2",   "Assembly",   "Line-D", "Station-2", "2020-09-18", "servo_motors,grippers,vision",  "Active"),
    ("MCH-011", "Press Brake P1",      "Press",      "Line-E", "Station-1", "2017-12-01", "hydraulic_ram,dies,backgauge",  "Active"),
    ("MCH-012", "Press Brake P2",      "Press",      "Line-E", "Station-2", "2018-03-10", "hydraulic_ram,dies,backgauge",  "Active"),
    ("MCH-013", "Welding Station W1",  "Welding",    "Line-E", "Station-3", "2021-08-25", "torch,wire_feed,gas_system",   "Active"),
    ("MCH-014", "Paint Booth PB1",     "Finishing",  "Line-F", "Station-1", "2019-05-12", "spray_guns,filters,exhaust",   "Active"),
    ("MCH-015", "CNC Grinder G1",     "CNC",       "Line-B", "Station-3", "2022-01-15", "wheel,spindle,coolant_system",  "Active"),
    ("MCH-016", "Heat Treat Oven H1", "Thermal",    "Line-F", "Station-2", "2018-11-20", "heating_elements,thermocouple", "Active"),
    ("MCH-017", "CMM Inspector 1",    "Inspection",  "Line-G", "Station-1", "2021-06-01", "probe,granite_table,software", "Active"),
    ("MCH-018", "Assembly Robot R3",   "Assembly",   "Line-D", "Station-3", "2023-01-10", "servo_motors,grippers,vision",  "Active"),
    ("MCH-019", "Deburring Cell D1",  "Finishing",  "Line-F", "Station-3", "2022-08-05", "brushes,spindle,coolant",       "Active"),
    ("MCH-020", "Leak Tester LT1",    "Testing",    "Line-G", "Station-2", "2020-12-15", "pressure_transducer,seals",     "Active"),
]

OPERATORS_DATA = [
    ("OP-001", "James Wilson",    "Morning",   "CNC Level 3,Safety", "2019-01-15", "Current"),
    ("OP-002", "Maria Garcia",    "Morning",   "CNC Level 3,Quality", "2018-06-01", "Current"),
    ("OP-003", "David Chen",      "Morning",   "Assembly Level 2,Safety", "2020-03-10", "Current"),
    ("OP-004", "Sarah Johnson",   "Morning",   "Injection Level 3", "2017-09-20", "Current"),
    ("OP-005", "Michael Brown",   "Morning",   "Welding Level 3,Safety", "2019-11-05", "Current"),
    ("OP-006", "Lisa Patel",      "Afternoon", "CNC Level 2", "2021-02-14", "Current"),
    ("OP-007", "Robert Kim",      "Afternoon", "CNC Level 3,Quality", "2018-08-22", "Current"),
    ("OP-008", "Emily Davis",     "Afternoon", "Assembly Level 3", "2019-04-18", "Current"),
    ("OP-009", "Thomas Martinez", "Afternoon", "Injection Level 2", "2020-07-01", "Current"),
    ("OP-010", "Jennifer Lee",    "Afternoon", "Finishing Level 2,Safety", "2021-05-30", "Current"),
    ("OP-011", "William Taylor",  "Night",     "CNC Level 2", "2022-01-10", "Current"),
    ("OP-012", "Amanda White",    "Night",     "CNC Level 1", "2022-06-15", "Current"),
    ("OP-013", "Christopher Ng",  "Night",     "Assembly Level 2", "2021-09-01", "Current"),
    ("OP-014", "Jessica Hernandez","Night",    "Assembly Level 1", "2024-07-01", "Current"),  # Scenario C
    ("OP-015", "Daniel Clark",    "Night",     "Welding Level 2", "2020-11-20", "Current"),
    ("OP-016", "Sophia Robinson", "Morning",   "Quality Level 3,CMM", "2018-03-01", "Current"),
    ("OP-017", "Andrew Lewis",    "Afternoon", "Maintenance Level 3", "2017-05-10", "Current"),
    ("OP-018", "Olivia Walker",   "Morning",   "Assembly Level 2", "2021-08-15", "Current"),
    ("OP-019", "Ryan Hall",       "Night",     "Assembly Level 1", "2024-08-15", "Current"),  # Scenario C
    ("OP-020", "Emma Wright",     "Afternoon", "Thermal Level 2", "2020-02-28", "Current"),
    ("OP-021", "Kevin Young",     "Morning",   "Press Level 3", "2019-07-12", "Current"),
    ("OP-022", "Rachel Allen",    "Night",     "Injection Level 1", "2023-03-01", "Current"),
    ("OP-023", "Mark King",       "Morning",   "Laser Level 2,Safety", "2020-10-05", "Current"),
    ("OP-024", "Laura Scott",     "Afternoon", "Assembly Level 2", "2021-12-01", "Current"),
    ("OP-025", "Jason Adams",     "Morning",   "Maintenance Level 2", "2022-04-10", "Current"),
    ("OP-026", "Nicole Baker",    "Afternoon", "Quality Level 2", "2021-03-20", "Current"),
    ("OP-027", "Brian Nelson",    "Night",     "CNC Level 2", "2022-09-15", "Current"),
    ("OP-028", "Megan Carter",    "Morning",   "Finishing Level 2", "2020-06-01", "Current"),
    ("OP-029", "Patrick Evans",   "Afternoon", "Testing Level 2", "2021-11-10", "Current"),
    ("OP-030", "Angela Torres",   "Night",     "Press Level 1", "2023-06-20", "Current"),
]

SUPPLIERS_DATA = [
    ("SUP-001", "SteelCorp Industries",  "Steel Alloy 4140",  "ISO 9001, IATF 16949", 4.5, 14),
    ("SUP-002", "PolyPlast Solutions",   "ABS Resin",          "ISO 9001",              4.1, 10),
    ("SUP-003", "PrecisionMet Ltd",      "Aluminum 6061-T6",   "ISO 9001, AS9100",     4.7, 12),
    ("SUP-004", "RawMat Global",         "Steel Alloy 4140",  "ISO 9001",              3.2, 21),  # Scenario B
    ("SUP-005", "BrassTech Components",  "Brass C360",         "ISO 9001",              4.4, 7),
    ("SUP-006", "CoatingsPro Inc",       "Powder Coat",        "ISO 9001",              4.0, 5),
    ("SUP-007", "AdhesiveBond Corp",     "Structural Adhesive","ISO 9001, IATF 16949", 4.6, 8),
    ("SUP-008", "WireForm Solutions",    "Welding Wire ER70S", "ISO 9001, AWS",        4.3, 10),
]

CUSTOMERS_DATA = [
    ("CUST-001", "AutoDrive Corp",      "Automotive",  "Detroit, MI",   "IATF 16949, PPAP Level 3"),
    ("CUST-002", "AeroJet Systems",     "Aerospace",   "Seattle, WA",   "AS9100D, FAI Required"),
    ("CUST-003", "MedDevice Global",    "Medical",     "Boston, MA",    "ISO 13485, FDA 21 CFR 820"),
    ("CUST-004", "HeavyMach Industries","Industrial",  "Houston, TX",   "ISO 9001"),
    ("CUST-005", "ElectroSys Inc",      "Electronics", "San Jose, CA",  "IPC-A-610 Class 3"),
]

PRODUCTS = [
    "SHAFT-A100", "SHAFT-A200", "BRACKET-B100", "HOUSING-C100",
    "GEAR-D100", "PLATE-E100", "CONNECTOR-F100", "VALVE-G100",
    "SLEEVE-H100", "MOUNT-I100",
]


def _generate_machines() -> pd.DataFrame:
    cols = ["machine_id", "name", "type", "line", "station", "install_date",
            "critical_components", "status"]
    return pd.DataFrame(MACHINES_DATA, columns=cols)


def _generate_operators() -> pd.DataFrame:
    cols = ["operator_id", "name", "shift", "certifications", "hire_date", "training_status"]
    return pd.DataFrame(OPERATORS_DATA, columns=cols)


def _generate_suppliers() -> pd.DataFrame:
    cols = ["supplier_id", "name", "material_type", "certification_level",
            "rating", "lead_time_days"]
    return pd.DataFrame(SUPPLIERS_DATA, columns=cols)


def _generate_customers() -> pd.DataFrame:
    cols = ["customer_id", "name", "industry", "plant_location", "quality_requirements"]
    return pd.DataFrame(CUSTOMERS_DATA, columns=cols)


def _generate_material_lots(days: int = 90) -> pd.DataFrame:
    """Generate incoming material lots from suppliers."""
    np.random.seed(42)
    records = []
    start = datetime(2025, 1, 1)
    lot_id = 1

    for day in range(0, days, 3):  # deliveries every ~3 days
        date = start + timedelta(days=day)
        for sup in random.sample(SUPPLIERS_DATA, k=random.randint(1, 4)):
            coa_status = "PASS"
            qty = random.randint(200, 2000)

            # Scenario B: SUP-004 bad batch weeks 8-9 (days 54-62)
            if sup[0] == "SUP-004" and 54 <= day <= 62:
                coa_status = "PASS"  # CoA passed but material is actually bad

            records.append({
                "lot_id": f"ML-{lot_id:04d}",
                "supplier_id": sup[0],
                "material": sup[2],
                "received_date": date.strftime("%Y-%m-%d"),
                "coa_status": coa_status,
                "qty": qty,
                "spec_grade": random.choice(["Grade-A", "Grade-B", "Grade-A"]),
            })
            lot_id += 1

    return pd.DataFrame(records)


def _generate_production_orders(material_lots_df: pd.DataFrame, days: int = 90) -> pd.DataFrame:
    np.random.seed(43)
    records = []
    start = datetime(2025, 1, 1)
    machines = [m[0] for m in MACHINES_DATA if m[2] in ("CNC", "Injection", "Press", "Assembly")]
    order_id = 1

    for day in range(days):
        date = start + timedelta(days=day)
        n_orders = random.randint(3, 8)
        for _ in range(n_orders):
            machine = random.choice(machines)
            product = random.choice(PRODUCTS)
            qty_planned = random.randint(50, 500)
            qty_produced = max(0, qty_planned + random.randint(-20, 5))
            due_date = date + timedelta(days=random.randint(3, 14))

            # Scenario A: MCH-005 output drops in weeks 5-8
            if machine == "MCH-005" and 35 <= day <= 56:
                degradation = (day - 35) / 21
                qty_produced = int(qty_produced * (1 - degradation * 0.15))

            records.append({
                "order_id": f"ORD-{order_id:04d}",
                "product_id": product,
                "machine_id": machine,
                "start_date": date.strftime("%Y-%m-%d"),
                "qty_planned": qty_planned,
                "qty_produced": qty_produced,
                "priority": random.choice(["Standard", "Standard", "Rush", "Critical"]),
                "due_date": due_date.strftime("%Y-%m-%d"),
                "status": "Completed",
            })
            order_id += 1

    return pd.DataFrame(records)


def _generate_bom_consumption(orders_df: pd.DataFrame, lots_df: pd.DataFrame) -> pd.DataFrame:
    """Link production orders to material lots consumed."""
    np.random.seed(44)
    records = []
    bom_id = 1
    lot_ids = lots_df["lot_id"].tolist()

    for _, order in orders_df.iterrows():
        n_materials = random.randint(1, 3)
        used_lots = random.sample(lot_ids, min(n_materials, len(lot_ids)))
        for lot in used_lots:
            records.append({
                "bom_id": f"BOM-{bom_id:05d}",
                "order_id": order["order_id"],
                "material_lot_id": lot,
                "qty_consumed": random.randint(10, 100),
                "station": random.choice(["Station-1", "Station-2", "Station-3"]),
            })
            bom_id += 1

    # Scenario B: Force SUP-004 bad lots into specific orders
    bad_lots = lots_df[
        (lots_df["supplier_id"] == "SUP-004") &
        (lots_df["received_date"] >= "2025-02-24") &
        (lots_df["received_date"] <= "2025-03-04")
    ]["lot_id"].tolist()

    if bad_lots:
        scenario_b_orders = orders_df[
            (orders_df["start_date"] >= "2025-02-26") &
            (orders_df["start_date"] <= "2025-03-10")
        ].sample(min(8, len(orders_df)), random_state=44)

        for _, order in scenario_b_orders.iterrows():
            lot = random.choice(bad_lots)
            records.append({
                "bom_id": f"BOM-{bom_id:05d}",
                "order_id": order["order_id"],
                "material_lot_id": lot,
                "qty_consumed": random.randint(20, 80),
                "station": "Station-1",
            })
            bom_id += 1

    return pd.DataFrame(records)


def _generate_sensor_readings(days: int = 90) -> pd.DataFrame:
    """Generate sensor time-series with embedded predictive signals."""
    np.random.seed(45)
    records = []
    start = datetime(2025, 1, 1)
    reading_id = 1
    machines_with_sensors = [m[0] for m in MACHINES_DATA if m[2] in ("CNC", "Injection", "Press")]

    for day in range(days):
        ts_base = start + timedelta(days=day)
        for machine_id in machines_with_sensors:
            for hour in range(0, 24, 2):  # reading every 2 hours
                ts = ts_base + timedelta(hours=hour)
                base_vib = 1.8 + random.gauss(0, 0.2)
                base_temp = 42.0 + random.gauss(0, 1.5)
                base_pressure = 6.2 + random.gauss(0, 0.3)
                base_power = 12.5 + random.gauss(0, 0.8)
                base_spindle = 3000 + random.gauss(0, 50)
                base_coolant = 4.5 + random.gauss(0, 0.2)

                # Scenario A: MCH-005 bearing degradation (vibration trending up weeks 3-8)
                if machine_id == "MCH-005" and day >= 21:
                    progress = min((day - 21) / 35.0, 1.0)
                    base_vib += progress * 3.2  # 1.8 -> 5.0 mm/s
                    base_temp += progress * 8.0  # heat from friction
                    base_power += progress * 2.5  # motor working harder

                records.append({
                    "reading_id": f"SR-{reading_id:07d}",
                    "machine_id": machine_id,
                    "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "vibration_mm_s": round(max(0.1, base_vib), 3),
                    "temp_c": round(base_temp, 2),
                    "pressure_bar": round(max(0.1, base_pressure), 2),
                    "power_kw": round(max(0.1, base_power), 2),
                    "spindle_rpm": round(max(100, base_spindle), 1),
                    "coolant_flow_lpm": round(max(0.1, base_coolant), 2),
                })
                reading_id += 1

    return pd.DataFrame(records)


def _generate_process_records(orders_df: pd.DataFrame, days: int = 90) -> pd.DataFrame:
    np.random.seed(46)
    records = []
    rec_id = 1
    shifts = {"Morning": ("OP-001", "OP-002", "OP-003", "OP-004", "OP-005"),
              "Afternoon": ("OP-006", "OP-007", "OP-008", "OP-009", "OP-010"),
              "Night": ("OP-011", "OP-012", "OP-013", "OP-014", "OP-015")}

    for _, order in orders_df.iterrows():
        shift = random.choice(["Morning", "Afternoon", "Night"])
        operator = random.choice(shifts[shift])
        day_num = (datetime.strptime(order["start_date"], "%Y-%m-%d") - datetime(2025, 1, 1)).days

        temp = 180.0 + random.gauss(0, 2.0)
        pressure = 45.0 + random.gauss(0, 1.5)
        speed = 2500 + random.gauss(0, 50)
        torque = 35.0 + random.gauss(0, 2.0)

        # Scenario C: Night shift Line-D adhesive temperature drift
        if order["machine_id"] in ("MCH-009", "MCH-010", "MCH-018") and shift == "Night":
            if operator in ("OP-014", "OP-019") and day_num >= 30:
                temp += random.uniform(3.0, 5.5)  # above spec

        records.append({
            "record_id": f"PR-{rec_id:06d}",
            "order_id": order["order_id"],
            "machine_id": order["machine_id"],
            "operator_id": operator,
            "shift": shift,
            "temperature_c": round(temp, 2),
            "pressure_bar": round(pressure, 2),
            "speed_rpm": round(speed, 1),
            "torque_nm": round(torque, 2),
            "timestamp": order["start_date"],
        })
        rec_id += 1

    return pd.DataFrame(records)


def _generate_quality_inspections(orders_df: pd.DataFrame, lots_df: pd.DataFrame,
                                   bom_df: pd.DataFrame, days: int = 90) -> pd.DataFrame:
    np.random.seed(47)
    records = []
    insp_id = 1
    inspectors = ["OP-016", "OP-026", "OP-029"]

    # Find orders linked to SUP-004 bad lots
    bad_lots = set(lots_df[
        (lots_df["supplier_id"] == "SUP-004") &
        (lots_df["received_date"] >= "2025-02-24") &
        (lots_df["received_date"] <= "2025-03-04")
    ]["lot_id"])
    bad_orders = set(bom_df[bom_df["material_lot_id"].isin(bad_lots)]["order_id"])

    for _, order in orders_df.iterrows():
        n_inspections = random.randint(2, 6)
        day_num = (datetime.strptime(order["start_date"], "%Y-%m-%d") - datetime(2025, 1, 1)).days

        for _ in range(n_inspections):
            nominal = 25.000
            usl = 25.050
            lsl = 24.950
            measured = nominal + random.gauss(0, 0.012)

            # Scenario A: MCH-005 dimensional drift
            if order["machine_id"] == "MCH-005" and day_num >= 28:
                drift = min((day_num - 28) / 28.0, 1.0) * 0.04
                measured += drift + random.gauss(0, 0.008)

            # Scenario B: SUP-004 surface porosity
            if order["order_id"] in bad_orders:
                if random.random() < 0.12:  # 12% defect rate
                    measured = nominal + random.uniform(0.06, 0.12)

            pass_fail = "PASS" if lsl <= measured <= usl else "FAIL"
            records.append({
                "inspection_id": f"QI-{insp_id:06d}",
                "order_id": order["order_id"],
                "inspector_id": random.choice(inspectors),
                "characteristic": random.choice(["Outer Diameter", "Length", "Surface Finish",
                                                  "Roundness", "Concentricity"]),
                "measured_value": round(measured, 4),
                "usl": usl,
                "lsl": lsl,
                "nominal": nominal,
                "pass_fail": pass_fail,
                "timestamp": order["start_date"],
            })
            insp_id += 1

    return pd.DataFrame(records)


def _generate_defect_records(inspections_df: pd.DataFrame) -> pd.DataFrame:
    np.random.seed(48)
    records = []
    def_id = 1
    failed = inspections_df[inspections_df["pass_fail"] == "FAIL"]

    for _, insp in failed.iterrows():
        defect_type = random.choice(["Dimensional", "Surface Porosity", "Surface Finish",
                                      "Structural Crack", "Bond Failure", "Cosmetic"])
        severity = random.choice(["Minor", "Minor", "Major", "Major", "Critical"])

        records.append({
            "defect_id": f"DEF-{def_id:06d}",
            "inspection_id": insp["inspection_id"],
            "order_id": insp["order_id"],
            "defect_type": defect_type,
            "severity": severity,
            "description": f"{defect_type} defect detected on {insp['characteristic']}: "
                           f"measured {insp['measured_value']}, spec [{insp['lsl']}, {insp['usl']}]",
            "containment_status": "Open",
        })
        def_id += 1

    return pd.DataFrame(records)


def _generate_maintenance_logs(days: int = 90) -> pd.DataFrame:
    np.random.seed(49)
    records = []
    log_id = 1
    start = datetime(2025, 1, 1)
    technicians = ["OP-017", "OP-025"]

    for m in MACHINES_DATA:
        # Scheduled PMs every 2 weeks
        for week in range(0, days // 14 + 1):
            pm_date = start + timedelta(weeks=week * 2) + timedelta(days=random.randint(0, 2))
            if (pm_date - start).days > days:
                break
            records.append({
                "log_id": f"MNT-{log_id:05d}",
                "machine_id": m[0],
                "type": "Scheduled",
                "description": random.choice([
                    "Routine lubrication", "Filter replacement", "Calibration check",
                    "Belt inspection", "Coolant change", "Sensor cleaning"]),
                "component_replaced": "None",
                "duration_hrs": round(random.uniform(0.5, 3.0), 1),
                "cost_usd": round(random.uniform(50, 400), 2),
                "technician_id": random.choice(technicians),
                "date": pm_date.strftime("%Y-%m-%d"),
            })
            log_id += 1

        # Unscheduled repairs
        for _ in range(random.randint(0, 3)):
            repair_date = start + timedelta(days=random.randint(0, days - 1))
            records.append({
                "log_id": f"MNT-{log_id:05d}",
                "machine_id": m[0],
                "type": "Unscheduled",
                "description": random.choice([
                    "Motor overheating", "Bearing noise", "Hydraulic leak",
                    "Electrical fault", "Spindle vibration", "Software error"]),
                "component_replaced": random.choice(["None", "Bearing", "Seal", "Filter", "Belt"]),
                "duration_hrs": round(random.uniform(1.0, 8.0), 1),
                "cost_usd": round(random.uniform(200, 5000), 2),
                "technician_id": random.choice(technicians),
                "date": repair_date.strftime("%Y-%m-%d"),
            })
            log_id += 1

    # Scenario A: MCH-005 last bearing replacement was 11 months ago
    records.append({
        "log_id": f"MNT-{log_id:05d}",
        "machine_id": "MCH-005",
        "type": "Scheduled",
        "description": "Bearing replacement - spindle bearings",
        "component_replaced": "Bearing",
        "duration_hrs": 4.0,
        "cost_usd": 1200.00,
        "technician_id": "OP-017",
        "date": "2024-02-15",
    })

    return pd.DataFrame(records)


def _generate_shipments(orders_df: pd.DataFrame) -> pd.DataFrame:
    np.random.seed(50)
    records = []
    ship_id = 1
    customers = [c[0] for c in CUSTOMERS_DATA]

    completed = orders_df[orders_df["status"] == "Completed"]
    for _, order in completed.iterrows():
        if random.random() < 0.75:  # 75% of orders shipped
            ship_date = datetime.strptime(order["start_date"], "%Y-%m-%d") + timedelta(days=random.randint(1, 5))
            records.append({
                "shipment_id": f"SHP-{ship_id:05d}",
                "order_id": order["order_id"],
                "customer_id": random.choice(customers),
                "ship_date": ship_date.strftime("%Y-%m-%d"),
                "qty": order["qty_produced"],
                "lot_number": f"LOT-2025-{ship_id:04d}",
                "destination": random.choice(["Detroit, MI", "Seattle, WA", "Boston, MA",
                                               "Houston, TX", "San Jose, CA"]),
                "tracking_status": "Delivered",
            })
            ship_id += 1

    return pd.DataFrame(records)


def _generate_parts_inventory() -> pd.DataFrame:
    np.random.seed(51)
    parts = [
        ("Spindle Bearing 6205",  "Bearing",    "MCH-001,MCH-002,MCH-003,MCH-004,MCH-005,MCH-015", 8, 5, 85.00),
        ("Spindle Bearing 6207",  "Bearing",    "MCH-005",                                          2, 3, 120.00),
        ("Chuck Jaw Set",         "Tooling",    "MCH-003,MCH-004,MCH-005",                         4, 2, 250.00),
        ("Coolant Pump Seal",     "Seal",       "MCH-001,MCH-002,MCH-015",                         12, 5, 15.00),
        ("Hydraulic Seal Kit",    "Seal",       "MCH-011,MCH-012",                                  6, 3, 45.00),
        ("Injection Screw",       "Wear Part",  "MCH-006,MCH-007",                                  1, 1, 1800.00),
        ("Laser Tube",            "Consumable", "MCH-008",                                           1, 1, 3500.00),
        ("Servo Motor Unit",      "Motor",      "MCH-009,MCH-010,MCH-018",                          2, 1, 2200.00),
        ("Welding Wire Spool",    "Consumable", "MCH-013",                                          20, 10, 35.00),
        ("Paint Filter Set",      "Filter",     "MCH-014",                                          15, 8, 22.00),
        ("Coolant Seal Ring",     "Seal",       "MCH-007",                                           0, 3, 28.00),  # OUT OF STOCK
        ("V-Belt Set",            "Belt",       "MCH-001,MCH-002,MCH-011,MCH-012",                  6, 4, 18.00),
        ("Probe Tip Ruby 2mm",   "Consumable", "MCH-017",                                           5, 3, 150.00),
        ("Grinding Wheel 200mm", "Consumable", "MCH-015",                                           3, 2, 280.00),
        ("Thermocouple K-Type",  "Sensor",     "MCH-016",                                           4, 2, 65.00),
    ]

    records = []
    for i, (name, cat, compat, qty, reorder, cost) in enumerate(parts, 1):
        records.append({
            "part_id": f"PART-{i:04d}",
            "part_name": name,
            "category": cat,
            "machine_compatibility": compat,
            "qty_on_hand": qty,
            "reorder_point": reorder,
            "unit_cost_usd": cost,
        })

    return pd.DataFrame(records)


# product -> (name, unit price USD); prices are what customers pay per unit
PRODUCT_INFO = {
    "SHAFT-A100": ("Drive shaft 25 mm", 38.0), "SHAFT-A200": ("Drive shaft 40 mm", 54.0),
    "BRACKET-B100": ("Mounting bracket", 14.5), "HOUSING-C100": ("Gearbox housing", 96.0),
    "GEAR-D100": ("Spur gear 48T", 42.0), "PLATE-E100": ("Base plate", 27.0),
    "CONNECTOR-F100": ("Hydraulic connector", 18.0), "VALVE-G100": ("Control valve body", 74.0),
    "SLEEVE-H100": ("Bearing sleeve", 22.0), "MOUNT-I100": ("Engine mount", 61.0),
}


def _generate_products() -> pd.DataFrame:
    return pd.DataFrame([{"product_id": p, "product_name": PRODUCT_INFO[p][0], "unit_price_usd": PRODUCT_INFO[p][1]}
                         for p in PRODUCTS])


def _generate_part_vendors(parts: pd.DataFrame) -> pd.DataFrame:
    """Who supplies each spare part, how long it takes and the minimum order."""
    vendors = {  # category -> (vendor, lead time days, MOQ, email)
        "Bearing": ("NorthStar Bearings", 5, 4, "orders@northstar-bearings.example"),
        "Tooling": ("Apex Tooling Supply", 10, 1, "sales@apextooling.example"),
        "Seal": ("SealTech Industrial", 3, 10, "orders@sealtech.example"),
        "Wear Part": ("Moldline Components", 21, 1, "service@moldline.example"),
        "Consumable": ("FactoryPro Consumables", 4, 5, "orders@factorypro.example"),
        "Motor": ("DriveCore Motion", 14, 1, "parts@drivecore.example"),
        "Filter": ("FactoryPro Consumables", 4, 10, "orders@factorypro.example"),
        "Belt": ("PowerTrans Belting", 2, 6, "orders@powertrans.example"),
        "Sensor": ("Thermon Sensors", 7, 2, "sales@thermon-sensors.example"),
    }
    long_lead = {"Laser Tube": 28, "Servo Motor Unit": 18}
    rows = []
    for p in parts.to_dict(orient="records"):
        v, lead, moq, mail = vendors[p["category"]]
        rows.append({"part_id": p["part_id"], "vendor_name": v, "lead_time_days": long_lead.get(p["part_name"], lead),
                     "min_order_qty": moq if p["unit_cost_usd"] < 500 else 1,
                     "unit_cost_usd": round(p["unit_cost_usd"] * 0.96, 2), "vendor_email": mail})
    return pd.DataFrame(rows)


def generate_all(days: int = 90) -> dict[str, pd.DataFrame]:
    """Generate all 13 tables and save to CSV."""
    print("Generating synthetic manufacturing data...")

    machines = _generate_machines()
    operators = _generate_operators()
    suppliers = _generate_suppliers()
    customers = _generate_customers()
    material_lots = _generate_material_lots(days)
    production_orders = _generate_production_orders(material_lots, days)
    bom_consumption = _generate_bom_consumption(production_orders, material_lots)
    sensor_readings = _generate_sensor_readings(days)
    process_records = _generate_process_records(production_orders, days)
    quality_inspections = _generate_quality_inspections(production_orders, material_lots,
                                                         bom_consumption, days)
    defect_records = _generate_defect_records(quality_inspections)
    maintenance_logs = _generate_maintenance_logs(days)
    shipments = _generate_shipments(production_orders)
    parts_inventory = _generate_parts_inventory()
    products = _generate_products()
    part_vendors = _generate_part_vendors(parts_inventory)

    tables = {
        "machines": machines,
        "operators": operators,
        "suppliers": suppliers,
        "customers": customers,
        "material_lots": material_lots,
        "production_orders": production_orders,
        "bom_consumption": bom_consumption,
        "sensor_readings": sensor_readings,
        "process_records": process_records,
        "quality_inspections": quality_inspections,
        "defect_records": defect_records,
        "maintenance_logs": maintenance_logs,
        "shipments": shipments,
        "parts_inventory": parts_inventory,
        "products": products,
        "part_vendors": part_vendors,
    }

    for name, df in tables.items():
        path = os.path.join(SYNTHETIC_DIR, f"{name}.csv")
        df.to_csv(path, index=False)
        print(f"  {name:25s} {len(df):>7,} rows  -> {path}")

    print(f"\nGenerated {len(tables)} tables.")
    return tables


# ---------------------------------------------------------------------------
# Live Simulator — generates new sensor data for demo monitoring
# ---------------------------------------------------------------------------

class LiveSimulator:
    """
    Generates non-deterministic sensor readings for live monitoring demos.

    - Each session picks a random degradation profile and target machine
    - Data evolves over time (not instant)
    - Stopping and resuming picks a NEW scenario
    """

    DEGRADATION_PROFILES = [
        {"name": "bearing_wear",   "sensor": "vibration_mm_s", "rate": 0.05, "threshold": 5.0},
        {"name": "thermal_drift",  "sensor": "temp_c",         "rate": 0.3,  "threshold": 65.0},
        {"name": "pressure_leak",  "sensor": "pressure_bar",   "rate": -0.02, "threshold": 3.0},
        {"name": "power_surge",    "sensor": "power_kw",       "rate": 0.08,  "threshold": 20.0},
        {"name": "coolant_loss",   "sensor": "coolant_flow_lpm","rate": -0.01, "threshold": 2.0},
    ]

    def __init__(self):
        self.active = False
        self.session_id = None
        self.profile = None
        self.target_machine = None
        self.step_count = 0
        self.baseline = {}

    def start(self):
        """Start a new monitoring session with a random scenario."""
        self.active = True
        self.session_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
        self.step_count = 0

        # Pick random scenario
        eligible_machines = [m[0] for m in MACHINES_DATA if m[2] in ("CNC", "Injection", "Press")]
        self.target_machine = random.choice(eligible_machines)
        self.profile = random.choice(self.DEGRADATION_PROFILES)

        self.baseline = {
            "vibration_mm_s": 1.8,
            "temp_c": 42.0,
            "pressure_bar": 6.2,
            "power_kw": 12.5,
            "spindle_rpm": 3000.0,
            "coolant_flow_lpm": 4.5,
        }

        return {
            "session_id": self.session_id,
            "target_machine": self.target_machine,
            "degradation_type": self.profile["name"],
        }

    def stop(self):
        """Stop monitoring. Next start() will pick a new scenario."""
        self.active = False
        self.session_id = None

    def generate_batch(self, batch_size: int = 10) -> list[dict]:
        """Generate a batch of sensor readings. Call this periodically."""
        if not self.active:
            return []

        readings = []
        machines_with_sensors = [m[0] for m in MACHINES_DATA if m[2] in ("CNC", "Injection", "Press")]
        now = datetime.now()

        for i in range(batch_size):
            ts = now + timedelta(seconds=i)
            for machine_id in random.sample(machines_with_sensors, min(3, len(machines_with_sensors))):
                reading = {
                    "machine_id": machine_id,
                    "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "vibration_mm_s": round(self.baseline["vibration_mm_s"] + random.gauss(0, 0.2), 3),
                    "temp_c": round(self.baseline["temp_c"] + random.gauss(0, 1.5), 2),
                    "pressure_bar": round(self.baseline["pressure_bar"] + random.gauss(0, 0.3), 2),
                    "power_kw": round(self.baseline["power_kw"] + random.gauss(0, 0.8), 2),
                    "spindle_rpm": round(self.baseline["spindle_rpm"] + random.gauss(0, 50), 1),
                    "coolant_flow_lpm": round(self.baseline["coolant_flow_lpm"] + random.gauss(0, 0.2), 2),
                }

                # Apply degradation to target machine
                if machine_id == self.target_machine:
                    sensor = self.profile["sensor"]
                    rate = self.profile["rate"]
                    reading[sensor] = round(reading[sensor] + rate * self.step_count, 3)

                readings.append(reading)

        self.step_count += 1
        return readings

    @property
    def is_alert(self) -> bool:
        """Check if the degradation has reached alert threshold."""
        if not self.active or not self.profile:
            return False
        sensor = self.profile["sensor"]
        current = self.baseline[sensor] + self.profile["rate"] * self.step_count
        threshold = self.profile["threshold"]
        if self.profile["rate"] > 0:
            return current >= threshold
        return current <= threshold

    @property
    def current_status(self) -> dict:
        if not self.active:
            return {"status": "inactive"}
        sensor = self.profile["sensor"]
        current = round(self.baseline[sensor] + self.profile["rate"] * self.step_count, 3)
        threshold = self.profile["threshold"]
        pct = abs(current - self.baseline[sensor]) / abs(threshold - self.baseline[sensor]) * 100
        return {
            "status": "monitoring",
            "session_id": self.session_id,
            "target_machine": self.target_machine,
            "degradation_type": self.profile["name"],
            "sensor": sensor,
            "current_value": current,
            "threshold": threshold,
            "progress_pct": round(min(pct, 100), 1),
            "alert": self.is_alert,
            "steps_elapsed": self.step_count,
        }


if __name__ == "__main__":
    tables = generate_all()
    print("\nRow counts:")
    for name, df in tables.items():
        print(f"  {name}: {len(df):,}")
