"""
Lot Traceability Tool.

Traces defective products through the manufacturing chain:
  Backward: defect -> order -> BOM -> material lot -> supplier
  Forward:  material lot -> BOM -> orders -> shipments -> customers

Works with both pandas DataFrames (local/DuckDB) and SQL queries (Snowflake).
"""

import pandas as pd


def trace_backward(defect_id: str, defects_df: pd.DataFrame,
                   inspections_df: pd.DataFrame, orders_df: pd.DataFrame,
                   bom_df: pd.DataFrame, lots_df: pd.DataFrame,
                   suppliers_df: pd.DataFrame) -> dict:
    """Trace a defect backward to its material and supplier origin."""
    defect = defects_df[defects_df["defect_id"] == defect_id]
    if defect.empty:
        return {"error": f"Defect {defect_id} not found"}

    defect = defect.iloc[0]
    order_id = defect["order_id"]

    order = orders_df[orders_df["order_id"] == order_id]
    order_info = order.iloc[0].to_dict() if not order.empty else {}

    bom_links = bom_df[bom_df["order_id"] == order_id]
    material_lots = []
    for _, link in bom_links.iterrows():
        lot = lots_df[lots_df["lot_id"] == link["material_lot_id"]]
        if not lot.empty:
            lot_info = lot.iloc[0].to_dict()
            sup = suppliers_df[suppliers_df["supplier_id"] == lot_info.get("supplier_id", "")]
            lot_info["supplier"] = sup.iloc[0].to_dict() if not sup.empty else {}
            material_lots.append(lot_info)

    return {
        "direction": "backward",
        "defect": defect.to_dict(),
        "production_order": order_info,
        "material_lots": material_lots,
        "supplier_count": len(set(l.get("supplier_id", "") for l in material_lots)),
    }


def trace_forward(material_lot_id: str, bom_df: pd.DataFrame,
                  orders_df: pd.DataFrame, shipments_df: pd.DataFrame,
                  customers_df: pd.DataFrame) -> dict:
    """Trace a material lot forward to all affected orders and customers."""
    bom_links = bom_df[bom_df["material_lot_id"] == material_lot_id]
    if bom_links.empty:
        return {"error": f"Material lot {material_lot_id} not found in BOM"}

    affected_orders = []
    affected_shipments = []
    affected_customers = set()

    for _, link in bom_links.iterrows():
        order = orders_df[orders_df["order_id"] == link["order_id"]]
        if not order.empty:
            affected_orders.append(order.iloc[0].to_dict())

            ships = shipments_df[shipments_df["order_id"] == link["order_id"]]
            for _, ship in ships.iterrows():
                ship_dict = ship.to_dict()
                cust = customers_df[customers_df["customer_id"] == ship["customer_id"]]
                if not cust.empty:
                    ship_dict["customer"] = cust.iloc[0].to_dict()
                    affected_customers.add(ship["customer_id"])
                affected_shipments.append(ship_dict)

    return {
        "direction": "forward",
        "material_lot_id": material_lot_id,
        "affected_orders": affected_orders,
        "affected_shipments": affected_shipments,
        "total_orders_affected": len(affected_orders),
        "total_shipments_affected": len(affected_shipments),
        "total_customers_affected": len(affected_customers),
        "total_units_at_risk": sum(o.get("qty_produced", 0) for o in affected_orders),
    }


def trace_full(defect_id: str, defects_df: pd.DataFrame,
               inspections_df: pd.DataFrame, orders_df: pd.DataFrame,
               bom_df: pd.DataFrame, lots_df: pd.DataFrame,
               suppliers_df: pd.DataFrame, shipments_df: pd.DataFrame,
               customers_df: pd.DataFrame) -> dict:
    """Full bidirectional trace from a defect."""
    backward = trace_backward(defect_id, defects_df, inspections_df,
                               orders_df, bom_df, lots_df, suppliers_df)

    if "error" in backward:
        return backward

    forward_results = []
    for lot in backward.get("material_lots", []):
        lot_id = lot.get("lot_id", "")
        if lot_id:
            fwd = trace_forward(lot_id, bom_df, orders_df, shipments_df, customers_df)
            forward_results.append(fwd)

    all_affected_orders = set()
    all_affected_customers = set()
    all_affected_shipments = []
    total_units = 0

    for fwd in forward_results:
        for o in fwd.get("affected_orders", []):
            all_affected_orders.add(o.get("order_id", ""))
        for s in fwd.get("affected_shipments", []):
            all_affected_shipments.append(s)
        total_units += fwd.get("total_units_at_risk", 0)
        for s in fwd.get("affected_shipments", []):
            cust = s.get("customer", {})
            if cust.get("customer_id"):
                all_affected_customers.add(cust["customer_id"])

    return {
        "defect_id": defect_id,
        "backward_trace": backward,
        "forward_traces": forward_results,
        "impact_summary": {
            "total_orders_affected": len(all_affected_orders),
            "total_shipments_affected": len(all_affected_shipments),
            "total_customers_affected": len(all_affected_customers),
            "total_units_at_risk": total_units,
            "containment_actions_needed": {
                "quarantine_orders": list(all_affected_orders),
                "notify_customers": list(all_affected_customers),
                "recall_shipments": [s.get("shipment_id", "") for s in all_affected_shipments],
            },
        },
    }
