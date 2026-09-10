"""Summarize saved manual order traffic offline, without publishing account values.

Does not send requests. Raw captures, credentials, quantities, prices and order
references remain in the private folder. Signature checks run locally.
"""
import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from mitmproxy import io

from probe_public_api import HOST, find_signing_key

ROOT = Path(__file__).resolve().parent
ENTRY = "/as_trade/api/order/v1/entrust_enter"
CANCEL = "/as_trade/api/order/v1/entrust_withdraw"
HISTORY = {"/as_trade/api/order/v1/get_today_entrust", "/as_trade/api/order/v1/get_all_entrust"}


def schema(value):
    if isinstance(value, dict):
        return {k: schema(v) for k, v in value.items()}
    if isinstance(value, list):
        return [schema(value[0])] if value else []
    return type(value).__name__


def utc(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def state_summary(row, stamp):
    # Status fields are enums. Free-text broker messages can contain identifying
    # values, so retain only the explicitly reviewed reason codes below.
    result = {"observed_utc": utc(stamp)}
    for key in ("entrustStatus", "entrustProp", "entrustBs", "timeInForce", "sessionType"):
        value = row.get(key)
        if isinstance(value, str) and len(value) <= 8 and value.isalnum():
            result[key] = value
    result["isOpen"] = row.get("isOpen") if isinstance(row.get("isOpen"), bool) else None
    amount = row.get("businessAmount")
    result["zero_fills"] = None if amount is None else Decimal(str(amount)) == 0
    reason = row.get("remark")
    if reason in ("NOT_TRADE_SESSION", "ORDER_EXECUTE_FAILED"):
        result["reason"] = reason
    elif reason:
        result["reason"] = "Unreviewed reason retained privately"
    return result


def summarize(since):
    observations = []
    with (ROOT / "private/zhuorui-flows.mitm").open("rb") as stream:
        for flow in io.FlowReader(stream).stream():
            if flow.request.host != HOST or flow.request.path not in {ENTRY, CANCEL, *HISTORY}:
                continue
            request = json.loads(flow.request.get_text(), parse_float=Decimal)
            response = json.loads(flow.response.get_text(), parse_float=Decimal) if flow.response else {}
            observations.append((flow, request, response))
    observations.sort(key=lambda item: item[0].request.timestamp_start)
    records = [
        (flow.request.timestamp_start, row)
        for flow, _, response in observations
        if flow.request.path in HISTORY and response.get("code") == "000000"
        and isinstance(response.get("data"), list)
        for row in response["data"] if isinstance(row, dict)
    ]
    result = {"verified_utc": utc(datetime.now(timezone.utc).timestamp()),
              "since_utc": utc(since), "submissions": [], "cancellations": [],
              "requests_sent_by_research_tools": 0}
    for flow, request, response in observations:
        stamp = flow.request.timestamp_start
        path = flow.request.path
        if stamp < since or path not in (ENTRY, CANCEL):
            continue
        unsigned = dict(request)
        sig = unsigned.pop("sign")
        find_signing_key(ROOT / "private/zhuorui.apk", unsigned, sig)
        item = {"time_utc": utc(stamp), "path": path,
                "http_status": flow.response.status_code if flow.response else None,
                "application_code": response.get("code"),
                "request_schema": schema(request), "response_schema": schema(response),
                "signature_verified_offline": True, "reconciled_states": []}
        if path == ENTRY:
            item["observed_instruction"] = {k: request[k] for k in
                ("entrustProp", "entrustBs", "ts", "allowPrePost", "apStatus", "volumeMultiple") if k in request}
            item["price_field_present"] = "entrustPrice" in request
            item["time_in_force_present"] = "timeInForce" in request
            refs = response.get("data") or {}
            if isinstance(refs, dict):
                for record_time, row in records:
                    if record_time >= stamp and all(refs.get(k) and row.get(k) == refs[k]
                                                     for k in ("orderNo", "orderTxnReference")):
                        state = state_summary(row, record_time)
                        state["matched_both_submission_references"] = True
                        item["reconciled_states"].append(state)
            result["submissions"].append(item)
        else:
            # The observed app cancellation supplies only orderTxnReference;
            # orderId and remark exist in the static class but are optional.
            ref = request.get("orderTxnReference")
            linked = [(prior, body) for prior, _, body in observations
                      if prior.request.path == ENTRY and prior.request.timestamp_start < stamp
                      and isinstance(body.get("data"), dict) and ref
                      and body["data"].get("orderTxnReference") == ref]
            item["matched_submission_times_utc"] = [utc(prior.request.timestamp_start) for prior, _ in linked]
            item["order_id_present"] = "orderId" in request
            item["remark_present"] = "remark" in request
            matches = []
            for record_time, row in records:
                if not ref or row.get("orderTxnReference") != ref:
                    continue
                matched_fields = [k for k in ("orderNo", "entrustNo", "orderId")
                                  if request.get("orderId") and row.get(k) == request["orderId"]]
                if request.get("orderId") and not matched_fields:
                    continue
                # When the submission is captured, check its second reference
                # too; a partial reference collision must not confirm a state.
                if linked and not any(body["data"].get("orderNo") and
                                      row.get("orderNo") == body["data"]["orderNo"] for _, body in linked):
                    continue
                matches.append({"observed_utc": utc(record_time), "orderId_matches_fields": matched_fields,
                                "transaction_reference_matched": True, "before_cancel": record_time < stamp})
                if record_time >= stamp:
                    item["reconciled_states"].append(state_summary(row, record_time))
            item["identifier_matches"] = matches
            result["cancellations"].append(item)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="UTC ISO timestamp of the manual session")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    start = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    if start.tzinfo is None:
        parser.error("--since must include a timezone")
    result = summarize(start.timestamp())
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"submissions": len(result["submissions"]), "cancellations": len(result["cancellations"]),
                      "signatures_verified_offline": True, "requests_sent": 0}))


if __name__ == "__main__":
    main()
