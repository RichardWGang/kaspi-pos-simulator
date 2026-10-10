#!/usr/bin/env python3
"""Симулятор Kaspi POS-терминала / Kaspi POS terminal simulator.

Запуск / Run:
    python kaspi_pos_simulator.py [--host H] [--port P] [--secure] [--cors] [--ssl]

Веб-консоль: http://host:port/ui
"""

import argparse
import datetime
import json
import logging
import os
import random
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("KaspiPOSSimulator")

# 控制台页眉显示的构建标记，用来确认浏览器连的是不是当前这份代码
UI_BUILD = "v5-approve5"

MOCK_DATA = {
    "storeName": "Тестовый магазин",
    "city": "г. Алматы",
    "address": "Конаева 11, 6",
    "bin": "960419351140",
    "terminalId": "31452963",
    "serialNum": "00043010171",
    "posNum": "0",
}

PHASE_INITIALIZE_SECONDS = 1.0
PHASE_WAIT_USER_SECONDS = 2.0
PHASE_PROCESSING_SECONDS = 4.0
PHASE_SETTLE_SECONDS = 6.0

# 手册 3.18：默认超时时间 —— 扫码 180 秒，支付确认 60 秒
SCAN_TIMEOUT_SECONDS = 180.0
CONFIRMATION_TIMEOUT_SECONDS = 60.0
STUCK_TRANSACTION_TIMEOUT = 300.0

TERMINAL_RETENTION_SECONDS = 86400  # 手册 3：已完成交易在内存中保存 24 小时
TOKEN_TTL_HOURS = 24

# 手册 3.19：状态更新请求的最大频率为每 10 秒一次
ACTUALIZE_MIN_INTERVAL_SECONDS = 10.0

OUTCOMES = ("success", "fail", "unknown")
# 手册 3.17 / 3.20：新版新增刷掌支付 alaqan
PAYMENT_METHODS = ("qr", "card", "alaqan")
# 手册 3.6 / 3.18：alaqan 与 qr 同属 Kaspi 钱包类，交易标识用 orderNumber
QR_LIKE_METHODS = ("qr", "alaqan")
# actualize 的模拟结果；nointernet 对应手册 3.22 的 statusCode 100
ACTUALIZE_OUTCOMES = ("success", "fail", "unknown", "nointernet")
TERMINAL_STATUSES = ("success", "fail", "unknown")

# 手册 3.18：subStatus 只允许这 11 个取值，不得自造
SUBSTATUS_ENUM = (
    "Initialize",
    "WaitUser",
    "WaitForQrConfirmation",
    "ProcessingCard",
    "WaitForPinCode",
    "ProcessRefund",
    "QrTransactionSuccess",
    "QrTransactionFailure",
    "CardTransactionSuccess",
    "CardTransactionFailure",
    "ProcessCancelled",
)

transactions = {}
# 已成功支付的台账，供退款校验使用：transactionId -> entry
ledger = {}
api_tokens = {}
# 最近一次注册（收银机调用 /v2/register 产生），供 /ui 展示设备与 token
last_registration = None
# 正在等控制台批准的注册请求（/ui 弹窗 允许/拒绝）；None 表示没有待审批的
pending_registration = None
# 没人点按钮时，注册请求最多挂多久（秒）
REGISTER_APPROVAL_TIMEOUT = 120.0
# 交易配置：收银机不指定 method / outcome / hold 时，模拟器按这里跑
# method=None 表示随机；hold=None 表示沿用 --manual-completion 的开关
trade_config = {"method": None, "outcome": "success", "hold": None}
_state_lock = threading.RLock()

_process_seq = 0
_process_seq_lock = threading.Lock()


def configure_logging(log_file, verbose=False):
    root = logging.getLogger("KaspiPOSSimulator")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    if log_file:
        try:
            handler = logging.FileHandler(log_file, encoding="utf-8")
            handler.setFormatter(fmt)
            root.addHandler(handler)
        except OSError as exc:
            print(f"Cannot open log file {log_file}: {exc}", file=sys.stderr)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)


def mask_secret(value):
    if not value:
        return "<empty>"
    if len(value) <= 12:
        return value[0] + "..." + value[-1]
    return f"{value[:6]}...{value[-4:]}"


def parse_flag(value, default=False):
    """把查询参数里的布尔开关解析成 True/False（1/true/yes/on 都算真）。"""
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def new_process_id():
    global _process_seq
    with _process_seq_lock:
        _process_seq = (_process_seq + 1) % 1000
        seq = _process_seq
    return f"{int(time.time() * 1000)}{seq:03d}"


def ledger_record(transaction):
    """成功完成的支付记入台账，供后续退款校验使用（手册 2.8 / 3.20）。"""
    if transaction.get("type") != "payment":
        return
    txn_id = transaction.get("transactionId")
    if not txn_id:
        return
    ledger[txn_id] = {
        "transactionId": txn_id,
        "method": transaction.get("method"),
        "amount": transaction["amount"],
        "refunded": 0,
        "reserved": 0,
        "createTime": transaction["createTime"],
    }


def available_refund_amount(entry):
    """可用退款金额 = 原金额 - 已退 - 在途预留（手册 3.20）。"""
    return entry["amount"] - entry["refunded"] - entry["reserved"]


def _settle_refund_reservation(transaction, outcome):
    """退款落定：成功计入已退金额，失败释放预留，unknown 保持预留。"""
    if transaction.get("type") != "refund":
        return
    entry = ledger.get(transaction.get("originalTransactionId"))
    if entry is None:
        return
    amount = transaction["amount"]
    entry["reserved"] = max(0, entry["reserved"] - amount)
    if outcome == "success":
        entry["refunded"] += amount


def _fill_cheque_fields(transaction):
    is_refund = transaction.get("type") == "refund"
    if transaction.get("method") != "card":
        transaction["subStatus"] = "QrTransactionSuccess"
        transaction.setdefault("orderNumber", str(uuid.uuid4().int)[:9])
        transaction["transactionId"] = transaction["orderNumber"]
        return
    transaction["subStatus"] = "CardTransactionSuccess"
    transaction.setdefault("rrn", str(uuid.uuid4().int)[:12])
    transaction.setdefault("authorizationCode", str(uuid.uuid4().int)[:6])
    transaction.setdefault("cardMask", f"440043******{str(uuid.uuid4().int)[:4]}")
    transaction["transactionId"] = transaction["rrn"]
    if not is_refund:
        transaction.setdefault("hostResponseCode", "000")
        transaction.setdefault("icc", "Visa Debit")


def _finalize_transaction(transaction, now):
    outcome = transaction.get("outcome", "success")
    is_card = transaction.get("method") == "card"
    if outcome == "fail":
        transaction["status"] = "fail"
        transaction["subStatus"] = (
            "CardTransactionFailure" if is_card else "QrTransactionFailure"
        )
        transaction.setdefault("message", "Отмена покупки; Покупатель отменил покупку")
    elif outcome == "unknown":
        transaction["status"] = "unknown"
        transaction["subStatus"] = "ProcessCancelled"
        transaction.setdefault("message", "Операция отменена")
    else:
        transaction["status"] = "success"
        _fill_cheque_fields(transaction)
    _settle_refund_reservation(transaction, outcome)
    if transaction["status"] == "success":
        ledger_record(transaction)
    transaction["completedTime"] = now


def _timeout_expired(transaction, now):
    """手册 3.18：扫码超时 180 秒，支付确认超时 60 秒。

    扫码阶段（客户尚未扫 QR / 刷卡）用扫码超时；扫码后的确认阶段用确认超时。
    """
    scan_time = transaction.get("scanTime")
    if scan_time is None:
        return (
            now - transaction["createTime"]
        ).total_seconds() > SCAN_TIMEOUT_SECONDS
    return (now - scan_time).total_seconds() > CONFIRMATION_TIMEOUT_SECONDS


def _finalize_timeout(transaction, now):
    transaction["status"] = "fail"
    transaction["subStatus"] = (
        "CardTransactionFailure"
        if transaction.get("method") == "card"
        else "QrTransactionFailure"
    )
    transaction["message"] = "Отмена покупки; Покупатель не подтвердил покупку"
    _settle_refund_reservation(transaction, "fail")
    transaction["completedTime"] = now
    logger.info("Transaction %s timed out", transaction["processId"])


def advance_transaction(transaction, now):
    if transaction["status"] != "wait":
        return
    elapsed = (now - transaction["createTime"]).total_seconds()
    if elapsed < PHASE_INITIALIZE_SECONDS:
        transaction["subStatus"] = "Initialize"
        return
    if transaction.get("method") is None:
        transaction["method"] = random.choice(PAYMENT_METHODS)
    if elapsed < PHASE_WAIT_USER_SECONDS:
        # 客户始终没有扫 QR / 刷卡 —— 走扫码超时（手册 3.18）
        if _timeout_expired(transaction, now):
            _finalize_timeout(transaction, now)
        else:
            transaction["subStatus"] = "WaitUser"
        return
    transaction.setdefault("scanTime", now)
    if transaction.get("type") == "refund":
        transaction["subStatus"] = "ProcessRefund"
    elif transaction["method"] == "card":
        transaction["subStatus"] = (
            "ProcessingCard" if elapsed < PHASE_PROCESSING_SECONDS else "WaitForPinCode"
        )
    else:
        transaction["subStatus"] = "WaitForQrConfirmation"
    if transaction.get("hold"):
        return  # 挂起的交易只等人点按钮，不自动落定也不自动超时
    if _timeout_expired(transaction, now):
        _finalize_timeout(transaction, now)
    elif elapsed >= PHASE_SETTLE_SECONDS:
        _finalize_transaction(transaction, now)


def reap_transactions(now):
    expired = []
    for process_id, transaction in transactions.items():
        if transaction["status"] == "wait":
            # hold 的交易是用户主动挂起的，不参与 300 秒兜底清理
            if transaction.get("hold"):
                continue
            stuck = (now - transaction["createTime"]).total_seconds()
            if stuck > STUCK_TRANSACTION_TIMEOUT:
                expired.append(process_id)
        elif transaction["status"] in TERMINAL_STATUSES:
            completed = transaction.setdefault("completedTime", now)
            if (now - completed).total_seconds() > TERMINAL_RETENTION_SECONDS:
                expired.append(process_id)
    for process_id in expired:
        del transactions[process_id]
    stale = [
        txn_id
        for txn_id, entry in ledger.items()
        if (now - entry["createTime"]).total_seconds() > TERMINAL_RETENTION_SECONDS
    ]
    for txn_id in stale:
        del ledger[txn_id]
    if expired:
        logger.info("Reaped %d expired transaction(s)", len(expired))
    return len(expired)


def cheque_info(transaction):
    is_refund = transaction.get("type") == "refund"
    info = {
        "storeName": MOCK_DATA["storeName"],
        "city": MOCK_DATA["city"],
        "address": MOCK_DATA["address"],
        "bin": MOCK_DATA["bin"],
        "terminalId": MOCK_DATA["terminalId"],
        "date": datetime.datetime.now().strftime("%d.%m.%y %H:%M:%S"),
        "method": transaction.get("method"),
        "type": transaction.get("type", "payment"),
        "amount": f"- {transaction['amount']} ₸" if is_refund else f"{transaction['amount']} ₸",
        "status": "Возврат успешно совершен" if is_refund else "Покупка с Kaspi Gold. Одобрено",
    }
    if transaction.get("method") == "card":
        info.update(
            {
                "cardMask": transaction.get("cardMask"),
                "rrn": transaction.get("rrn"),
                "authorizationCode": transaction.get("authorizationCode"),
            }
        )
        if not is_refund:
            info["icc"] = transaction.get("icc", "Visa Debit")
            info["hostResponseCode"] = transaction.get("hostResponseCode", "000")
    else:
        info["orderNumber"] = transaction.get("orderNumber")
    return info


class KaspiPOSSimulatorHandler(BaseHTTPRequestHandler):
    server_version = "KaspiPOSSimulator/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logger.debug("HTTP %s", fmt % args)

    def log_error(self, fmt, *args):
        logger.debug("HTTP error %s", fmt % args)

    def _cors_headers(self):
        if getattr(self.server, "cors_enabled", False):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "accesstoken, content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def _send_json(self, payload, http_status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(http_status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_error(self, business_code, error_text, http_status=200, data_message=None):
        """业务码只放 body；HTTP 码按手册 3.22 另给（400/401/403/404/500）。"""
        logger.error(
            "Error: %s (statusCode: %s, HTTP: %s)", error_text, business_code, http_status
        )
        payload = {"errorText": error_text, "statusCode": business_code}
        if data_message:
            payload["data"] = {"message": data_message}
        self._send_json(payload, http_status=http_status)

    def _header(self, name):
        wanted = name.lower()
        for header_name, value in self.headers.items():
            if header_name.lower() == wanted:
                return value
        return None

    def _extract_token(self):
        token = self._header("accesstoken")
        if token:
            return token
        values = parse_qs(urlparse(self.path).query).get("accesstoken")
        return values[0] if values else None

    def _authenticate(self):
        """通过返回 None；否则返回 (业务码, errorText, HTTP 码)。"""
        if not getattr(self.server, "secure_api", False):
            return None
        token = self._extract_token()
        if not token:
            logger.error("accesstoken missing from headers and query")
            return (999, "Authorization failed", 401)
        with _state_lock:
            record = api_tokens.get(token)
        if record is None:
            logger.error("Unknown access token %s", mask_secret(token))
            return (999, "Authorization failed", 401)
        if datetime.datetime.now() >= record["expiration"]:
            logger.error("Access token %s expired", mask_secret(token))
            return (999, "Session expires and need to be updated", 403)
        return None

    def _needs_auth(self, path):
        if not getattr(self.server, "secure_api", False):
            return False
        if path == "/v2/reset" and getattr(self.server, "allow_open_reset", False):
            return False
        return True

    def do_OPTIONS(self):
        if not getattr(self.server, "cors_enabled", False):
            return self._handle_error(
                999, "Invalid Url. Please, contact developers", http_status=404
            )
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        logger.info("Path: %s, Params: %s", path, params)

        if path == "/ui":
            return self._serve_ui()
        if path == "/ui/transactions":
            return self._handle_ui_transactions()
        if path == "/ui/ledger":
            return self._handle_ui_ledger()
        if path == "/ui/complete":
            return self._handle_ui_complete(params)
        if path == "/ui/config":
            return self._handle_ui_config(params)
        if path == "/ui/registration":
            return self._handle_ui_registration()
        if path == "/ui/registration/approve":
            return self._handle_ui_registration_decision(True)
        if path == "/ui/registration/reject":
            return self._handle_ui_registration_decision(False)

        if path != "/v2/register" and self._needs_auth(path):
            failure = self._authenticate()
            if failure:
                return self._handle_error(*failure)

        routes = {
            "/v2/payment": self._handle_payment,
            "/v2/refund": self._handle_refund,
            "/v2/remains": self._handle_remains,
            "/v2/status": self._handle_status,
            "/v2/actualize": self._handle_actualize,
            "/v2/deviceinfo": self._handle_device_info,
            "/v2/register": self._handle_register,
            "/v2/revoke": self._handle_revoke,
            "/v2/reset": self._handle_reset,
        }
        handler = routes.get(path)
        if handler is None:
            return self._handle_error(
                999, "Invalid Url. Please, contact developers", http_status=404
            )
        return handler(params)

    def _serve_ui(self):
        if not getattr(self.server, "ui_enabled", True):
            return self._handle_error(
                999, "Invalid Url. Please, contact developers", http_status=404
            )
        secure = "true" if getattr(self.server, "secure_api", False) else "false"
        manual = "true" if getattr(self.server, "manual_completion", False) else "false"
        self._send_html(
            UI_HTML.replace("__SECURE_API__", secure)
            .replace("__MANUAL_MODE__", manual)
            .replace("__UI_BUILD__", UI_BUILD)
        )

    def _handle_ui_transactions(self):
        now = datetime.datetime.now()
        with _state_lock:
            reap_transactions(now)
            items = []
            ordered = sorted(
                transactions.values(), key=lambda t: t["createTime"], reverse=True
            )
            for txn in ordered:
                advance_transaction(txn, now)
                items.append(
                    {
                        "processId": txn["processId"],
                        "status": txn["status"],
                        "subStatus": txn["subStatus"],
                        "amount": txn["amount"],
                        "method": txn.get("method"),
                        "type": txn.get("type", "payment"),
                        "outcome": txn.get("outcome", "success"),
                        "hold": bool(txn.get("hold")),
                        "transactionId": txn.get("transactionId"),
                        "createTime": txn["createTime"].isoformat(timespec="seconds"),
                    }
                )
        self._send_json({"data": items, "statusCode": 0})

    def _handle_ui_ledger(self):
        """可退款台账，供控制台退款表单选择（只含已成功且未退完的支付）。"""
        with _state_lock:
            items = [
                {
                    "transactionId": entry["transactionId"],
                    "method": entry["method"],
                    "amount": entry["amount"],
                    "refunded": entry["refunded"],
                    "reserved": entry["reserved"],
                    "available": available_refund_amount(entry),
                }
                for entry in sorted(
                    ledger.values(), key=lambda e: e["createTime"], reverse=True
                )
            ]
        self._send_json({"data": items, "statusCode": 0})

    def _handle_ui_complete(self, params):
        """把挂起的交易按指定结果落定（控制台「完成支付 / 完成退款」按钮）。"""
        if "processId" not in params:
            return self._handle_error(999, "Missing param: processId", http_status=400)
        process_id = params["processId"]
        outcome = params.get("outcome", "success")
        if outcome not in OUTCOMES:
            return self._handle_error(
                999,
                "Param unvalidated: outcome. Expected one of " + ", ".join(OUTCOMES),
                http_status=400,
            )
        now = datetime.datetime.now()
        with _state_lock:
            transaction = transactions.get(process_id)
            if transaction is None:
                return self._handle_error(101, "Process not found")
            if transaction["status"] != "wait":
                return self._handle_error(
                    105,
                    "Error acquired in service process",
                    data_message="Could not complete. Transaction is not waiting",
                )
            transaction.pop("hold", None)
            transaction["outcome"] = outcome
            _finalize_transaction(transaction, now)
        logger.info("Manually completed %s -> %s", process_id, outcome)
        return self._send_status(process_id)

    def _handle_ui_config(self, params):
        """交易配置的读写：决定收银机发起的交易怎么跑（只影响未显式传参的字段）。"""
        if "method" in params:
            method = params["method"]
            if method and method != "auto" and method not in PAYMENT_METHODS:
                return self._handle_error(
                    999, "Param unvalidated: method. Unknown payment method",
                    http_status=400,
                )
            trade_config["method"] = None if method in ("", "auto") else method
        if "outcome" in params:
            outcome = params["outcome"]
            if outcome not in OUTCOMES:
                return self._handle_error(
                    999,
                    "Param unvalidated: outcome. Expected one of " + ", ".join(OUTCOMES),
                    http_status=400,
                )
            trade_config["outcome"] = outcome
        if "hold" in params:
            raw = str(params["hold"]).strip().lower()
            trade_config["hold"] = None if raw in ("", "auto") else parse_flag(raw)
        logger.info("Trade config updated: %s", trade_config)
        self._send_json({"data": dict(trade_config), "statusCode": 0})

    def _handle_ui_registration(self):
        """控制台只读展示最近一次注册 + 正在等待批准的注册请求。"""
        timeout = getattr(self.server, "register_timeout", REGISTER_APPROVAL_TIMEOUT)
        with _state_lock:
            record = dict(last_registration) if last_registration else None
            pending = None
            if pending_registration is not None:
                left = timeout - (
                    datetime.datetime.now() - pending_registration["requestedAt"]
                ).total_seconds()
                pending = {
                    "id": pending_registration["id"],
                    "name": pending_registration["name"],
                    "requestedAt": pending_registration["requestedAt"].strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "expiresIn": max(0, int(left)),
                }
        if record is None:
            self._send_json({"data": None, "pending": pending, "statusCode": 0})
            return
        active = not record["revoked"] and record["expiration"] > datetime.datetime.now()
        self._send_json(
            {
                "data": {
                    "deviceName": record["name"],
                    "deviceId": record["deviceId"],
                    "accessToken": record["access_token"],
                    "refreshToken": record["refresh_token"],
                    "expirationDate": record["expiration"].strftime("%Y-%m-%d %H:%M:%S"),
                    "registeredAt": record["registeredAt"].strftime("%Y-%m-%d %H:%M:%S"),
                    "active": active,
                },
                "pending": pending,
                "statusCode": 0,
            }
        )

    def _handle_ui_registration_decision(self, approved):
        """控制台弹窗的「允许注册 / 拒绝」：放行或否掉挂起中的注册请求。"""
        global pending_registration
        with _state_lock:
            request = pending_registration
            if request is not None:
                request["approved"] = approved
                pending_registration = None
        if request is None:
            return self._handle_error(
                999, "No registration request is waiting for confirmation",
                http_status=400,
            )
        request["event"].set()
        logger.info(
            "Registration request %s %s", request["id"],
            "approved" if approved else "rejected",
        )
        self._send_json(
            {"data": {"name": request["name"], "approved": approved}, "statusCode": 0}
        )

    def _read_amount(self, params):
        if "amount" not in params:
            self._handle_error(999, "Missing param: amount", http_status=400)
            return None
        try:
            amount = int(params["amount"])
        except ValueError:
            self._handle_error(
                999, "Param unvalidated: amount. Must be a number", http_status=400
            )
            return None
        if amount <= 0:
            self._handle_error(
                999, "Param unvalidated: amount. Must be greater 0", http_status=400
            )
            return None
        return amount

    def _guard_no_active_transaction(self):
        now = datetime.datetime.now()
        with _state_lock:
            reap_transactions(now)
            for txn in transactions.values():
                if txn["status"] != "wait":
                    continue
                if (now - txn["createTime"]).total_seconds() > STUCK_TRANSACTION_TIMEOUT:
                    continue
                self._handle_error(
                    107,
                    "Could not register transaction. Last operation was not completed.",
                )
                return False
        return True

    def _resolve_hold(self, params):
        """这笔交易要不要挂起等待人工确认。

        优先级：请求里的 hold > /ui 的交易配置 > 服务端模式（--manual-completion）。
        点餐机/收银机发来的普通请求（不带任何参数）也会按配置被挂起，必须点按钮才落定。
        """
        default = getattr(self.server, "manual_completion", False)
        if trade_config.get("hold") is not None:
            default = trade_config["hold"]
        return parse_flag(params.get("hold"), default=default)

    def _handle_payment(self, params):
        amount = self._read_amount(params)
        if amount is None:
            return
        if not self._guard_no_active_transaction():
            return
        method = params.get("method")
        if method is None:
            method = trade_config.get("method")  # 收银机没指定就用 /ui 的交易配置
        if method is not None and method not in PAYMENT_METHODS:
            return self._handle_error(
                999, "Param unvalidated: method. Unknown payment method", http_status=400
            )
        outcome = params.get("outcome", trade_config.get("outcome", "success"))
        if outcome not in OUTCOMES:
            return self._handle_error(
                999,
                "Param unvalidated: outcome. Expected one of " + ", ".join(OUTCOMES),
                http_status=400,
            )
        hold = self._resolve_hold(params)
        # 挂起模式下没人轮询就没有机会随机，创建时先定好方式（同 v2 修复 #4）
        if method is None and hold:
            method = random.choice(PAYMENT_METHODS)

        process_id = new_process_id()
        with _state_lock:
            transactions[process_id] = {
                "processId": process_id,
                "status": "wait",
                "subStatus": "Initialize",
                "amount": amount,
                "method": method,
                "outcome": outcome,
                "createTime": datetime.datetime.now(),
                "owncheque": params.get("owncheque", "false").lower() == "true",
                "type": "payment",
                "hold": hold,
            }
        logger.info(
            "Registered payment %s amount=%s method=%s outcome=%s hold=%s",
            process_id,
            amount,
            method or "auto",
            outcome,
            hold,
        )
        self._send_json(
            {"data": {"processId": process_id, "status": "wait"}, "statusCode": 0}
        )

    def _validate_refund(self, transaction_id, method, amount):
        """手册 2.8 / 3.3 / 3.20：退款必须与原支付方式一致，且不得超过可用退款金额。

        返回 None 表示通过，否则返回应写入 errorText 的文案。
        """
        with _state_lock:
            entry = ledger.get(transaction_id)
            if entry is None:
                return "Param unvalidated: transactionId. Transaction not found"
            if method != entry["method"]:
                return (
                    "Param unvalidated: method. Refund method must match the original "
                    f"payment method ({entry['method']})"
                )
            available = available_refund_amount(entry)
            if amount > available:
                return (
                    "Param unvalidated: amount. Refund amount exceeds available "
                    f"amount ({available})"
                )
        return None

    def _handle_refund(self, params, source="/v2/refund"):
        for param in ("amount", "method", "transactionId"):
            if param not in params:
                return self._handle_error(
                    999, f"Missing param: {param}", http_status=400
                )
        amount = self._read_amount(params)
        if amount is None:
            return
        method = params["method"]
        if method not in PAYMENT_METHODS:
            return self._handle_error(
                999, "Param unvalidated: method. Unknown payment method", http_status=400
            )
        outcome = params.get("outcome", trade_config.get("outcome", "success"))
        if outcome not in OUTCOMES:
            return self._handle_error(
                999,
                "Param unvalidated: outcome. Expected one of " + ", ".join(OUTCOMES),
                http_status=400,
            )
        if getattr(self.server, "refund_validation", True):
            problem = self._validate_refund(params["transactionId"], method, amount)
            if problem:
                logger.warning("Refund rejected: %s", problem)
                return self._handle_error(999, problem, http_status=400)
        if not self._guard_no_active_transaction():
            return
        hold = self._resolve_hold(params)

        process_id = new_process_id()
        with _state_lock:
            entry = ledger.get(params["transactionId"])
            if entry is not None:
                entry["reserved"] += amount
            transactions[process_id] = {
                "processId": process_id,
                "status": "wait",
                "subStatus": "Initialize",
                "amount": amount,
                "method": method,
                "outcome": outcome,
                "transactionId": params["transactionId"],
                "originalTransactionId": params["transactionId"],
                "createTime": datetime.datetime.now(),
                "owncheque": params.get("owncheque", "false").lower() == "true",
                "type": "refund",
                "hold": hold,
            }
        logger.info(
            "Registered refund %s amount=%s method=%s against %s via %s hold=%s",
            process_id, amount, method, params["transactionId"], source, hold,
        )
        self._send_json(
            {"data": {"processId": process_id, "status": "wait"}, "statusCode": 0}
        )

    def _handle_status(self, params):
        if "processId" not in params:
            return self._handle_error(
                999, "Missing param: processId", http_status=400
            )
        # 手册 3.18：/v2/status 的请求头同时要求 accessToken 与 terminalId
        if getattr(self.server, "require_terminal_id", True):
            terminal_id = self._header("terminalid")
            if not terminal_id:
                return self._handle_error(
                    999, "Missing header: terminalId", http_status=400
                )
            if terminal_id != MOCK_DATA["terminalId"]:
                return self._handle_error(
                    999,
                    "Param unvalidated: terminalId. Unknown terminal",
                    http_status=400,
                )
        return self._send_status(params["processId"])

    def _send_status(self, process_id):
        """构造 /v2/status 的响应；actualize 之后复用同一结构（手册 3.19）。"""
        now = datetime.datetime.now()
        with _state_lock:
            transaction = transactions.get(process_id)
            if transaction is None:
                return self._handle_error(101, "Process not found")
            advance_transaction(transaction, now)
            if transaction["status"] == "success":
                data = {
                    "processId": process_id,
                    "status": "success",
                    "subStatus": transaction["subStatus"],
                    "transactionId": transaction.get("transactionId"),
                    "addInfo": {"IsOffer": False, "LoanTerm": 0, "ProductType": "Gold"},
                    "chequeInfo": cheque_info(transaction),
                }
            elif transaction["status"] in ("fail", "unknown"):
                data = {
                    "message": transaction.get("message", "Операция отменена"),
                    "processId": process_id,
                    "status": transaction["status"],
                    "subStatus": transaction["subStatus"],
                }
            else:
                data = {
                    "processId": process_id,
                    "status": "wait",
                    "subStatus": transaction["subStatus"],
                }
        self._send_json({"data": data, "statusCode": 0})

    def _handle_remains(self, params):
        """手册未正式定义 /v2/remains（仅在错误码 107/108 与 3.19 中被提及）。

        按「退余款」实现：只给 transactionId 不给 amount 时，退还该笔交易的
        剩余可退金额；给出 amount 时等价于 /v2/refund。
        """
        if "transactionId" not in params:
            return self._handle_error(
                999, "Missing param: transactionId", http_status=400
            )
        params = dict(params)
        params.pop("processId", None)
        with _state_lock:
            entry = ledger.get(params["transactionId"])
        if entry is None:
            return self._handle_error(
                999,
                "Param unvalidated: transactionId. Transaction not found",
                http_status=400,
            )
        if "amount" not in params:
            remaining = available_refund_amount(entry)
            if remaining <= 0:
                return self._handle_error(
                    999,
                    "Param unvalidated: amount. Nothing left to refund",
                    http_status=400,
                )
            params["amount"] = remaining
        if "method" not in params:
            params["method"] = entry["method"]
        return self._handle_refund(params, source="/v2/remains")

    def _handle_actualize(self, params):
        if "processId" not in params:
            return self._handle_error(
                999, "Missing param: processId", http_status=400
            )
        outcome = params.get("outcome", "success")
        if outcome not in ACTUALIZE_OUTCOMES:
            return self._handle_error(
                999,
                "Param unvalidated: outcome. Expected one of "
                + ", ".join(ACTUALIZE_OUTCOMES),
                http_status=400,
            )
        process_id = params["processId"]
        now = datetime.datetime.now()
        with _state_lock:
            transaction = transactions.get(process_id)
            if transaction is None:
                return self._handle_error(101, "Process not found")
            if transaction["status"] != "unknown":
                return self._handle_error(103, "This transaction is unable to actualize")
            # 手册 3.19：状态更新请求的最大频率为每 10 秒一次
            last = transaction.get("lastActualizeTime")
            if last is not None:
                waited = (now - last).total_seconds()
                if waited < ACTUALIZE_MIN_INTERVAL_SECONDS:
                    logger.warning(
                        "Actualize %s throttled (%.1fs since last call)",
                        process_id, waited,
                    )
                    return self._handle_error(
                        105,
                        "Error acquired in service process",
                        data_message=(
                            "Actualize Error. Max frequency is 1 request per "
                            f"{ACTUALIZE_MIN_INTERVAL_SECONDS:.0f} seconds"
                        ),
                    )
            transaction["lastActualizeTime"] = now
            if outcome == "nointernet":
                # 手册 3.22：httpCode 200 / statusCode 100，交易保持 unknown
                logger.info("Actualize %s simulated network failure", process_id)
                return self._handle_error(100, "No internet")
            if outcome == "unknown":
                # 手册 3.19：若该请求仍返回 unknown，可稍后再次发起请求
                transaction["status"] = "unknown"
                transaction["subStatus"] = "ProcessCancelled"
                transaction["message"] = "Операция отменена"
                logger.info("Actualize %s still unknown", process_id)
            else:
                transaction["outcome"] = outcome
                transaction.pop("message", None)
                _finalize_transaction(transaction, now)
                logger.info("Actualized transaction %s -> %s", process_id, outcome)
        return self._send_status(process_id)

    def _handle_device_info(self, params=None):
        self._send_json(
            {
                "data": {
                    "posNum": MOCK_DATA["posNum"],
                    "serialNum": MOCK_DATA["serialNum"],
                    "terminalId": MOCK_DATA["terminalId"],
                },
                "statusCode": 0,
            }
        )

    def _handle_register(self, params):
        """收银机（点餐机）注册：控制台同意后才签发 token。"""
        if "name" not in params:
            return self._handle_error(999, "Missing param: name", http_status=400)
        if getattr(self.server, "register_approval", True):
            if not self._await_registration_approval(params["name"]):
                return
        self._issue_token(params["name"])

    def _client_alive(self):
        """探测收银机是否还连着（用 MSG_PEEK，不消耗数据）。

        收银机往往有自己的超时并会先断开，而模拟器原本会一直傻等到
        register_timeout 秒、期间 pending 请求不释放 —— /ui 的弹窗就一直挂着
        （那个请求其实已经没人要了）。只有明确读到 EOF 才判定断开。
        """
        sock = getattr(self, "connection", None)
        if sock is None:
            return True
        try:
            sock.setblocking(False)
            try:
                data = sock.recv(1, socket.MSG_PEEK)
            finally:
                sock.setblocking(True)
        except Exception:  # noqa: BLE001 - 探测不了就当还活着，避免误杀等待中的收银机
            return True
        return data != b""  # b"" 表示对端已关闭

    def _await_registration_approval(self, name):
        """把这次注册挂起，等控制台在 /ui 弹窗里点「允许注册 / 拒绝」。

        返回 True 表示放行；False 表示失败响应已经发出去了（或请求已被丢弃）。
        """
        global pending_registration
        request = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "event": threading.Event(),
            "approved": False,
            "requestedAt": datetime.datetime.now(),
        }
        timeout = getattr(self.server, "register_timeout", REGISTER_APPROVAL_TIMEOUT)
        with _state_lock:
            busy = pending_registration is not None
            if not busy:
                pending_registration = request
        if busy:
            self._handle_error(
                107,
                "Could not register. Another registration is waiting for "
                "confirmation in /ui",
                data_message="Register Error. Pending confirmation",
            )
            return False
        logger.info(
            "Registration request %s for %s: waiting for the /ui confirmation "
            "(timeout %gs)", request["id"], name, timeout,
        )
        answered = False
        client_gone = False
        deadline = time.time() + timeout
        while True:
            if request["event"].wait(0.5):
                answered = True
                break
            if time.time() >= deadline:
                break
            if not self._client_alive():
                client_gone = True
                break
        with _state_lock:
            if pending_registration is request:
                pending_registration = None
        if client_gone:
            # 收银机已经断开：这次请求没人要了，别让 /ui 弹窗继续挂着
            logger.warning(
                "Registration request %s dropped: the cashier disconnected",
                request["id"],
            )
            self.close_connection = True
            return False
        if not answered:
            self._handle_error(
                105,
                "Registration timed out: nobody confirmed it in /ui",
                data_message="Register Error. No confirmation",
            )
            return False
        if not request["approved"]:
            self._handle_error(
                999,
                "Registration rejected by the operator in /ui",
                http_status=400,
            )
            return False
        logger.info("Registration request %s approved", request["id"])
        return True

    def _issue_token(self, name):
        """签发 token 并记住这次注册，供 /ui 展示设备与 token。"""
        access_token = uuid.uuid4().hex
        refresh_token = uuid.uuid4().hex
        device_id = MOCK_DATA["terminalId"]  # 设备ID 就是终端号，与 /v2/deviceinfo 一致
        expiration = datetime.datetime.now() + datetime.timedelta(hours=TOKEN_TTL_HOURS)
        with _state_lock:
            global last_registration
            api_tokens[access_token] = {
                "name": name,
                "refresh_token": refresh_token,
                "expiration": expiration,
                "device_id": device_id,
            }
            last_registration = {
                "name": name,
                "deviceId": device_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expiration": expiration,
                "registeredAt": datetime.datetime.now(),
                "revoked": False,
            }
        logger.info(
            "Issued token %s for %s (device %s)",
            mask_secret(access_token), name, device_id,
        )
        self._send_json(
            {
                "data": {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "deviceName": name,
                    "deviceId": device_id,
                    "expirationDate": expiration.strftime("%Y-%m-%d %H:%M:%S"),
                },
                "statusCode": 0,
            }
        )

    def _handle_revoke(self, params):
        global last_registration
        if "name" not in params:
            return self._handle_error(999, "Missing param: name", http_status=400)
        if "refreshToken" not in params:
            return self._handle_error(
                999, "Param unvalidated: refreshToken. It's empty", http_status=400
            )
        current_token = self._extract_token()
        with _state_lock:
            record = api_tokens.get(current_token) if current_token else None
            if record is None:
                # 手册 3.16 未要求 revoke 带 accesstoken；非 secure 模式按 refreshToken 定位。
                # 启用 --secure 时缺少/无效令牌才按 401 处理。
                if getattr(self.server, "secure_api", False):
                    return self._handle_error(
                        999, "Authorization failed", http_status=401
                    )
                record = next(
                    (
                        r
                        for r in api_tokens.values()
                        if r["refresh_token"] == params["refreshToken"]
                    ),
                    None,
                )
            if record is None:
                return self._handle_error(
                    105,
                    "Error acquired in service process",
                    data_message="Revoke Error. Wrong refresh token",
                )
            if record["name"] != params["name"]:
                return self._handle_error(
                    105,
                    "Error acquired in service process",
                    data_message="Revoke Error. Wrong name",
                )
            if record["refresh_token"] != params["refreshToken"]:
                return self._handle_error(
                    105,
                    "Error acquired in service process",
                    data_message="Revoke Error. Wrong refresh token",
                )
            new_access_token = uuid.uuid4().hex
            new_refresh_token = uuid.uuid4().hex
            expiration = datetime.datetime.now() + datetime.timedelta(
                hours=TOKEN_TTL_HOURS
            )
            if last_registration and (
                last_registration["access_token"] == current_token
                or last_registration["refresh_token"] == params["refreshToken"]
            ):
                last_registration["revoked"] = True
            api_tokens.pop(current_token, None)
            for stale_token, stale in list(api_tokens.items()):
                if stale is record:
                    del api_tokens[stale_token]
            api_tokens[new_access_token] = {
                "name": params["name"],
                "refresh_token": new_refresh_token,
                "expiration": expiration,
            }
        logger.info("Rotated token for %s", params["name"])
        self._send_json(
            {
                "data": {
                    "accessToken": new_access_token,
                    "refreshToken": new_refresh_token,
                    "expirationDate": expiration.strftime("%Y-%m-%d %H:%M:%S"),
                },
                "statusCode": 0,
            }
        )

    def _handle_reset(self, params):
        with _state_lock:
            active = sum(1 for t in transactions.values() if t["status"] == "wait")
            total = len(transactions)
            ledger_count = len(ledger)
            transactions.clear()
            ledger.clear()
        logger.info(
            "Reset all transactions. Active: %d, Total: %d, Ledger: %d",
            active, total, ledger_count,
        )
        self._send_json(
            {
                "data": {
                    "message": (
                        f"All transactions have been reset. Active: {active}, "
                        f"Total: {total}, Refundable: {ledger_count}"
                    )
                },
                "statusCode": 0,
            }
        )


def find_openssl():
    """定位可用的 openssl（Windows 上 Git 自带的那个也算）。"""
    found = shutil.which("openssl")
    if found:
        return found
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Git\usr\bin\openssl.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Git\usr\bin\openssl.exe"),
        r"C:\Program Files\Git\usr\bin\openssl.exe",
    ]
    portable = os.path.join(
        os.path.expanduser("~"), ".workbuddy", "binaries", "PortableGit", "versions"
    )
    if os.path.isdir(portable):
        for entry in sorted(os.listdir(portable)):
            candidates.append(os.path.join(portable, entry, "usr", "bin", "openssl.exe"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def primary_lan_ip():
    """尝试取本机局域网 IP，写进自签证书的 SAN。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def ensure_self_signed_cert(cert_path, key_path, days=365):
    """生成自签证书（手册 2.5 / 3.1：集成必须通过 HTTPS）。

    返回 cert_path，失败返回 None。真实 Kaspi POS 用的是
    *.kaspipos.kz 的 CA 证书，这里只能自签，客户端需关闭证书校验。
    """
    openssl = find_openssl()
    if not openssl:
        return None
    directory = os.path.dirname(os.path.abspath(cert_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    san = ["DNS:localhost", "DNS:kaspipos.local", "IP:127.0.0.1"]
    lan_ip = primary_lan_ip()
    if lan_ip:
        san.append(f"IP:{lan_ip}")
    command = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key_path, "-out", cert_path, "-days", str(days),
        "-subj", "/CN=kaspipos.local",
        "-addext", "subjectAltName=" + ",".join(san),
    ]
    try:
        proc = subprocess.run(command, capture_output=True)
    except OSError as exc:
        logger.debug("openssl unavailable: %s", exc)
        return None
    if proc.returncode != 0 or not (
        os.path.isfile(cert_path) and os.path.isfile(key_path)
    ):
        logger.debug(
            "openssl failed: %s", proc.stderr.decode("utf-8", "replace")[:300]
        )
        return None
    logger.info("Generated self-signed certificate at %s", cert_path)
    return cert_path


class KaspiSimulatorServer(ThreadingHTTPServer):
    """HTTP/HTTPS 服务器。

    若配置了 TLS，则在 accept 之后才握手，这样「用 http:// 访问 HTTPS 端口」时能
    回一个可读的 400 提示，而不是直接 RST（浏览器只显示 ERR_CONNECTION_RESET）。
    """

    def __init__(self, server_address, handler, tls_context=None):
        self.tls_context = tls_context
        super().__init__(server_address, handler)

    def get_request(self):
        sock, client_address = self.socket.accept()
        if self.tls_context is None:
            return sock, client_address
        # 必须在握手「之前」判断：wrap_socket 失败时会直接关闭底层 socket
        # （fileno() 变成 -1），那时已经发不出任何响应了。
        if self._is_plain_http(sock):
            self._hint_plain_http(sock)
            raise OSError("plain HTTP request received on HTTPS port")
        return self.tls_context.wrap_socket(sock, server_side=True), client_address

    @staticmethod
    def _is_plain_http(sock):
        """TLS 握手的首字节必然是 0x16（Handshake 记录），明文请求则是 ASCII。"""
        try:
            sock.settimeout(0.5)
            first = sock.recv(1, socket.MSG_PEEK)
        except OSError:
            return False
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass
        return bool(first) and first[0] != 0x16

    @staticmethod
    def _hint_plain_http(sock):
        body = (
            "This port serves HTTPS only.\r\n"
            "Open https://<host>:<port>/ui instead of http://...\r\n"
            "(Restart with --http if you want plain HTTP.)\r\n"
        ).encode("utf-8")
        head = (
            "HTTP/1.1 400 Bad Request\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        try:
            sock.sendall(head + body)
        except OSError:
            return
        # 先半关闭再把残留的请求字节读完：否则 Windows 会因为「关闭时仍有未读数据」
        # 直接发 RST，把刚发出的响应一起丢掉，客户端只看到连接被重置。
        try:
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(0.5)
            while sock.recv(4096):
                pass
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def run(
    host="0.0.0.0",
    port=8080,
    secure_api=False,
    cors_enabled=False,
    ui_enabled=True,
    allow_open_reset=False,
    cert=None,
    key=None,
    refund_validation=True,
    require_terminal_id=True,
    manual_completion=False,
    register_approval=True,
    register_timeout=REGISTER_APPROVAL_TIMEOUT,
):
    tls_context = None
    if cert and key:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(certfile=cert, keyfile=key)

    try:
        httpd = KaspiSimulatorServer(
            (host, port), KaspiPOSSimulatorHandler, tls_context=tls_context
        )
    except OSError as exc:
        raise SystemExit(
            f"Cannot listen on {host}:{port} ({exc}).\n"
            "Another instance is probably still running on this port - stop it "
            "first (or start with another --port), otherwise the browser keeps "
            "talking to the OLD process and its pending registration stays."
        ) from exc
    httpd.secure_api = secure_api
    httpd.cors_enabled = cors_enabled
    httpd.ui_enabled = ui_enabled
    httpd.allow_open_reset = allow_open_reset
    httpd.refund_validation = refund_validation
    httpd.require_terminal_id = require_terminal_id
    httpd.manual_completion = manual_completion
    httpd.register_approval = register_approval
    httpd.register_timeout = register_timeout

    scheme = "https" if cert else "http"
    logger.info("Starting Kaspi POS simulator on %s://%s:%d", scheme, host, port)
    if cert:
        logger.info(
            "HTTPS enabled (manual 2.5) - plain HTTP requests to this port will fail"
        )
    else:
        logger.warning(
            "Serving plain HTTP - the manual requires HTTPS; use --http only for local debugging"
        )
    if secure_api:
        logger.info("API security enabled: send the accesstoken header")
    if not refund_validation:
        logger.warning(
            "Refund validation disabled - method/amount/transactionId are not checked"
        )
    if not require_terminal_id:
        logger.warning(
            "terminalId header check disabled - /v2/status accepts any terminal"
        )
    if manual_completion:
        logger.info(
            "Manual completion ON - every new payment/refund (whatever the caller "
            "sends) waits for the Complete button in /ui or a /ui/complete call"
        )
    if register_approval:
        logger.info(
            "Registration approval ON - /v2/register waits for the Allow button in "
            "/ui (%gs timeout); disable with --no-register-approval", register_timeout
        )
    if host in ("0.0.0.0", "::"):
        logger.warning(
            "Listening on all interfaces - anyone on the LAN can reach this simulator"
        )
    if ui_enabled:
        logger.info("Web console: %s://%s:%d/ui", scheme, host, port)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Kaspi POS simulator...")
    finally:
        httpd.server_close()
        logger.info("Server stopped.")


def build_parser():
    parser = argparse.ArgumentParser(description="Kaspi POS Terminal Simulator")
    parser.add_argument("--host", default="0.0.0.0", help="Host to listen on (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    parser.add_argument("--secure", action="store_true", help="Require accesstoken on every endpoint except /v2/register")
    parser.add_argument("--allow-open-reset", action="store_true", help="Keep /v2/reset open even with --secure")
    parser.add_argument("--no-refund-validation", action="store_true",
                        help="Skip the manual 2.8/3.20 refund checks (method match, "
                             "available amount, known transactionId)")
    parser.add_argument("--no-ui", action="store_true", help="Disable the built-in web console")
    parser.add_argument("--cors", action="store_true", help="Send permissive CORS headers")
    parser.add_argument("--http", action="store_true",
                        help="Serve plain HTTP instead of the default HTTPS")
    parser.add_argument("--ssl", action="store_true",
                        help="Require HTTPS; fails instead of falling back to HTTP")
    parser.add_argument("--cert", help="PEM certificate to use for HTTPS")
    parser.add_argument("--key", help="PEM private key to use for HTTPS")
    parser.add_argument("--cert-dir",
                        help="Where to cache the generated self-signed certificate "
                             "(default: <script dir>/.certs)")
    parser.add_argument("--no-terminal-check", action="store_true",
                        help="Do not require the terminalId header on /v2/status (manual 3.18)")
    parser.add_argument("--manual-completion", action="store_true",
                        help="Hold every new payment/refund (even plain requests from a cash "
                             "register) until the Complete button in /ui or /ui/complete is used; "
                             "a request can still override with hold=0")
    parser.add_argument("--no-register-approval", action="store_true",
                        help="Issue the token straight away instead of waiting for the "
                             "Allow button in /ui (default: ask /ui before registering)")
    parser.add_argument("--register-timeout", type=float, default=REGISTER_APPROVAL_TIMEOUT,
                        help="How long a registration request waits for the /ui "
                             "Allow/Reject button before failing (default: 120)")
    parser.add_argument("--scan-timeout", type=float, default=SCAN_TIMEOUT_SECONDS,
                        help="QR scan timeout in seconds (manual 3.18 default: 180)")
    parser.add_argument("--confirm-timeout", type=float, default=CONFIRMATION_TIMEOUT_SECONDS,
                        help="Payment confirmation timeout in seconds (manual 3.18 default: 60)")
    parser.add_argument("--fast", action="store_true",
                        help="Compress all timings for quick manual testing")
    parser.add_argument("--store-name", help="Override storeName in chequeInfo")
    parser.add_argument("--terminal-id", help="Override terminalId")
    parser.add_argument("--log-file", default="kaspi_pos_simulator.log", help="Log file path (empty to disable)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser


def apply_timings(args):
    """把命令行上的超时/加速开关写回模块常量。"""
    global PHASE_INITIALIZE_SECONDS, PHASE_WAIT_USER_SECONDS
    global PHASE_PROCESSING_SECONDS, PHASE_SETTLE_SECONDS
    global SCAN_TIMEOUT_SECONDS, CONFIRMATION_TIMEOUT_SECONDS
    if args.fast:
        PHASE_INITIALIZE_SECONDS = 0.1
        PHASE_WAIT_USER_SECONDS = 0.3
        PHASE_PROCESSING_SECONDS = 0.6
        PHASE_SETTLE_SECONDS = 1.0
        SCAN_TIMEOUT_SECONDS = 6.0
        CONFIRMATION_TIMEOUT_SECONDS = 3.0
    if not args.fast:
        SCAN_TIMEOUT_SECONDS = args.scan_timeout
        CONFIRMATION_TIMEOUT_SECONDS = args.confirm_timeout
    if CONFIRMATION_TIMEOUT_SECONDS < PHASE_SETTLE_SECONDS:
        CONFIRMATION_TIMEOUT_SECONDS = PHASE_SETTLE_SECONDS
    if SCAN_TIMEOUT_SECONDS < CONFIRMATION_TIMEOUT_SECONDS:
        SCAN_TIMEOUT_SECONDS = CONFIRMATION_TIMEOUT_SECONDS


def resolve_certificate(args):
    """默认走 HTTPS（手册 2.5）：自动生成自签证书；失败时按参数决定是否降级。

    返回 (cert, key)；两者都为 None 表示走明文 HTTP。
    """
    if args.http:
        return None, None
    if args.cert and args.key:
        if not (os.path.isfile(args.cert) and os.path.isfile(args.key)):
            raise SystemExit(f"--cert/--key not found: {args.cert} / {args.key}")
        return args.cert, args.key
    cert_dir = args.cert_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".certs"
    )
    cert_path = os.path.join(cert_dir, "cert.pem")
    key_path = os.path.join(cert_dir, "key.pem")
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return cert_path, key_path
    generated = ensure_self_signed_cert(cert_path, key_path)
    if generated:
        return cert_path, key_path
    if args.ssl:
        raise SystemExit(
            "HTTPS requested but no certificate available and openssl was not found.\n"
            "Install Git for Windows (ships openssl) or provide --cert/--key, "
            "or start with --http to serve plain HTTP."
        )
    logger.warning(
        "openssl not found - falling back to plain HTTP. "
        "The manual (2.5) requires HTTPS for the real integration."
    )
    return None, None


def main(argv=None):
    args = build_parser().parse_args(argv)
    configure_logging(args.log_file, verbose=args.verbose)

    if args.store_name:
        MOCK_DATA["storeName"] = args.store_name
    if args.terminal_id:
        MOCK_DATA["terminalId"] = args.terminal_id

    apply_timings(args)
    cert, key = resolve_certificate(args)

    run(
        host=args.host,
        port=args.port,
        secure_api=args.secure,
        cors_enabled=args.cors,
        ui_enabled=not args.no_ui,
        allow_open_reset=args.allow_open_reset,
        cert=cert,
        key=key,
        refund_validation=not args.no_refund_validation,
        require_terminal_id=not args.no_terminal_check,
        manual_completion=args.manual_completion,
        register_approval=not args.no_register_approval,
        register_timeout=args.register_timeout,
    )
    return 0


UI_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kaspi POS 模拟器控制台</title>
<style>
:root{
  --bg:#f5f6f8; --card:#fff; --line:#e4e7ec; --text:#1f2328; --muted:#6b7280;
  --accent:#2563eb; --ok:#15803d; --warn:#b45309; --bad:#b91c1c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif}
header{background:#fff;border-bottom:1px solid var(--line);padding:14px 20px;
  display:flex;align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:16px;font-weight:600;margin:0}
.pill{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
.pill.on{color:var(--ok);border-color:#bbf7d0;background:#f0fdf4}
.pill.off{color:var(--bad);border-color:#fecaca;background:#fef2f2}
main{max-width:1180px;margin:0 auto;padding:20px;display:grid;gap:16px;
  grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card h2{font-size:13px;font-weight:600;margin:0 0 12px;color:var(--muted);
  text-transform:uppercase;letter-spacing:.04em}
label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}
input,select,button{font:inherit}
input,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--text)}
input:focus,select:focus{outline:2px solid #bfdbfe;border-color:var(--accent)}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row>*{flex:1 1 140px}
button{padding:8px 14px;border-radius:6px;border:1px solid var(--line);background:#fff;
  color:var(--text);cursor:pointer}
button:hover{border-color:#c7ccd4}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{background:#1d4ed8}
button.ghost{padding:5px 10px;font-size:12px}
.badge{display:inline-block;padding:3px 10px;border-radius:6px;font-size:12px;font-weight:600;
  border:1px solid var(--line);color:var(--muted);background:#f9fafb}
.badge.wait{color:var(--warn);border-color:#fde68a;background:#fffbeb}
.badge.success{color:var(--ok);border-color:#bbf7d0;background:#f0fdf4}
.badge.fail,.badge.unknown{color:var(--bad);border-color:#fecaca;background:#fef2f2}
.kv{display:grid;grid-template-columns:96px 1fr;gap:6px 10px;font-size:13px;margin:0}
.kv dt{color:var(--muted)}
.kv dd{margin:0;font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500}
td.id{font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--muted)}
tr.cur td{background:#eff6ff}
pre{margin:0;background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;
  font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;overflow:auto;max-height:340px;white-space:pre-wrap}
.steps{display:flex;gap:6px;margin:12px 0 4px}
.step{flex:1;text-align:center;font-size:11px;padding:5px 4px;border-radius:6px;
  background:#f3f4f6;color:var(--muted);border:1px solid var(--line)}
.step.active{background:#eff6ff;color:var(--accent);border-color:#bfdbfe}
.step.done{background:#f0fdf4;color:var(--ok);border-color:#bbf7d0}
.hidden{display:none}
.hint{font-size:12px;color:var(--muted);margin:8px 0 0}
.err{color:var(--bad);font-size:12px;margin:8px 0 0;min-height:18px}
.secure{background:#fffbeb;border-color:#fde68a}
.modal{position:fixed;inset:0;background:rgba(15,23,42,.45);display:flex;align-items:center;
  justify-content:center;z-index:50}
.modal .box{background:#fff;border-radius:12px;padding:22px;max-width:400px;width:90%;
  box-shadow:0 12px 32px rgba(0,0,0,.22)}
.modal h3{margin:0 0 10px;font-size:16px}
.modal p{margin:0 0 8px;font-size:13px}
.modal .btns{display:flex;gap:10px;margin-top:16px}
/* 必须比 .modal 更具体：.modal 的 display:flex 声明在 .hidden 之后，
   同为单类选择器会盖掉 .hidden 的 display:none，弹窗就再也关不掉 */
.modal.hidden{display:none}
</style>
</head>
<body>
<header>
  <h1>Kaspi POS 模拟器</h1>
  <span class="pill" id="build">build __UI_BUILD__</span>
  <span class="pill" id="mode">模式</span>
  <span class="pill" id="conn">连接中…</span>
  <span class="pill" id="term">terminal: —</span>
  <span style="flex:1"></span>
  <button class="ghost" id="btnReset">重置全部交易</button>
</header>

<main>
  <section class="card" id="secureCard">
    <h2>设备注册信息（收银机调用 /v2/register）</h2>
    <dl class="kv" id="regKv">
      <dt>设备名称</dt><dd id="rName">—</dd>
      <dt>设备ID（终端号）</dt><dd id="rDevice" class="id">—</dd>
      <dt>accesstoken</dt><dd id="rToken" class="id">—</dd>
      <dt>refreshToken</dt><dd id="rRefresh" class="id">—</dd>
      <dt>有效期至</dt><dd id="rExp">—</dd>
      <dt>注册时间</dt><dd id="rTime">—</dd>
    </dl>
    <label>页面当前使用的 accesstoken</label>
    <input id="token" placeholder="收银机注册后自动填充，也可手动粘贴">
    <p class="hint" id="tokenHint">注册由收银机发起，页面只展示并自动使用拿到的 token。</p>
  </section>

  <section class="card">
    <h2>交易配置（交易由收银机发起）</h2>
    <div class="row">
      <div><label>支付方式（收银机未指定时）</label>
        <select id="cfgMethod">
          <option value="auto">自动随机</option>
          <option value="qr">Kaspi QR</option>
          <option value="card">银行卡 card</option>
          <option value="alaqan">刷掌 alaqan</option>
        </select>
      </div>
      <div><label>交易结果（收银机未指定时）</label>
        <select id="cfgOutcome">
          <option value="success">成功 success</option>
          <option value="fail">失败 fail</option>
          <option value="unknown">未知 unknown</option>
        </select>
      </div>
    </div>
    <label style="display:flex;align-items:center;gap:6px;margin-top:10px">
      <input type="checkbox" id="cfgHold" style="width:auto">
      <span>手动完成：交易停在 wait，需点「完成支付 / 完成退款」才落定</span>
    </label>
    <p style="margin:14px 0 0"><button class="primary" id="btnSaveCfg">保存配置</button></p>
    <p class="hint" id="cfgHint">只影响收银机没显式传参的字段：请求里带了 method / outcome / hold 时以请求为准。</p>
    <p class="err" id="cfgErr"></p>
  </section>

  <section class="card">
    <h2>当前交易</h2>
    <div class="steps" id="steps">
      <div class="step" data-s="wait">① 等待用户</div>
      <div class="step" data-s="proc">② 处理中</div>
      <div class="step" data-s="done">③ 完成</div>
    </div>
    <dl class="kv" id="curKv">
      <dt>processId</dt><dd id="kPid">—</dd>
      <dt>状态</dt><dd><span class="badge" id="kStatus">—</span></dd>
      <dt>subStatus</dt><dd id="kSub">—</dd>
      <dt>金额</dt><dd id="kAmount">—</dd>
      <dt>方式</dt><dd id="kMethod">—</dd>
      <dt>交易号</dt><dd id="kTxn">—</dd>
    </dl>
    <p style="margin:12px 0 0" class="row">
      <button class="ghost" id="btnRefresh">立即查询</button>
      <button class="ghost" id="btnActualize">补确认 actualize</button>
    </p>
    <div class="row" style="margin-top:10px">
      <div><label>手动完成的结果</label>
        <select id="doneOutcome">
          <option value="success">成功 success</option>
          <option value="fail">失败 fail</option>
          <option value="unknown">未知 unknown</option>
        </select>
      </div>
    </div>
    <p style="margin:12px 0 0" class="row">
      <button class="primary" id="btnDonePay">完成支付</button>
      <button class="primary" id="btnDoneRefund">完成退款</button>
    </p>
    <p class="hint" id="doneHint">开启「手动完成」后，收银机发起的交易停在 wait，点上面按钮按所选结果落定。</p>
    <div class="row" style="margin-top:8px">
      <div><label>actualize 模拟结果（手册 3.19：仍可能返回 unknown）</label>
        <select id="actOutcome">
          <option value="success">成功 success</option>
          <option value="fail">失败 fail</option>
          <option value="unknown">仍未知 unknown（10 秒后可重试）</option>
          <option value="nointernet">网络错误 No internet（statusCode 100）</option>
        </select>
      </div>
    </div>
    <p class="err" id="curErr"></p>
  </section>

  <section class="card">
    <h2>交易列表</h2>
    <table>
      <thead><tr><th>processId</th><th>类型</th><th>金额</th><th>状态</th><th>subStatus</th></tr></thead>
      <tbody id="listBody"><tr><td colspan="5" style="color:var(--muted)">暂无交易</td></tr></tbody>
    </table>
    <p class="hint">点击一行可切换查看该交易。</p>
  </section>

  <section class="card" style="grid-column:1/-1">
    <h2>最近一次响应 <button class="ghost" id="btnCopy" style="float:right">复制</button></h2>
    <pre id="raw">—</pre>
  </section>
</main>

<div class="modal hidden" id="approveModal">
  <div class="box">
    <h3 id="apTitle">允许这次注册？</h3>
    <p id="apName">—</p>
    <p class="hint" id="apHint">只有收银软件调用 /v2/register 才会出现这个提示。点「允许注册」后
      才签发 token；点「拒绝」则本次注册失败。</p>
    <p class="err" id="apErr"></p>
    <div class="btns">
      <button class="primary" id="btnAllow">允许注册</button>
      <button class="ghost" id="btnDeny">拒绝</button>
      <button class="ghost" id="btnDismiss">稍后处理</button>
    </div>
  </div>
</div>

<script>
var SECURE = __SECURE_API__;
var MANUAL = __MANUAL_MODE__;
var token = localStorage.getItem('kaspi_token') || '';
var current = null, timer = null, listTimer = null, LEDGER = [], TERMINAL = '', TXNS = {};

var LAST_PATH = '';
function $(id){ return document.getElementById(id); }
function hdr(){
  var h = {};
  if (token) h.accesstoken = token;
  if (TERMINAL) h.terminalid = TERMINAL;   // 手册 3.18：/v2/status 必填
  return h;
}
function show(node, obj){
  // 注意 UI_HTML 是 Python 字符串，别在这里写转义序列，用 charCode 拼换行
  node.textContent = 'GET ' + LAST_PATH + String.fromCharCode(10, 10) +
    JSON.stringify(obj, null, 2);
}

async function call(path){
  LAST_PATH = path;
  var res = await fetch(path, {headers: hdr(), cache: 'no-store'});
  var body = await res.json();
  return {http: res.status, body: body};
}
function check(r){
  if (r.body && r.body.statusCode && r.body.statusCode !== 0){
    throw new Error('[' + r.body.statusCode + '] ' + (r.body.errorText || ''));
  }
  return r.body;
}

function setConn(ok){
  var el = $('conn');
  el.textContent = ok ? '服务已连接' : '无法连接服务';
  el.className = 'pill ' + (ok ? 'on' : 'off');
}

async function loadDevice(){
  try{
    var d = check(await call('/v2/deviceinfo'));
    TERMINAL = d.data.terminalId;
    $('term').textContent = 'terminal: ' + TERMINAL;
    setConn(true);
  }catch(e){ setConn(false); $('term').textContent = 'terminal: —'; }
}

function renderSteps(status){
  var map = {wait:0, proc:1, done:2};
  var idx = status === 'wait' ? 0 : 2;
  var sub = $('kSub').textContent;
  var mid = {ProcessingCard:1, WaitForQrConfirmation:1, ProcessRefund:1, WaitForPinCode:1};
  if (status === 'wait' && mid[sub]) idx = 1;
  if (status === 'unknown') idx = 1;
  ['wait','proc','done'].forEach(function(s, i){
    var el = document.querySelector('.step[data-s="' + s + '"]');
    el.className = 'step' + (i < idx ? ' done' : i === idx ? ' active' : '');
  });
}

function renderCurrent(d){
  $('kPid').textContent = d.processId || '—';
  $('kStatus').textContent = d.status || '—';
  $('kStatus').className = 'badge ' + (d.status || '');
  $('kSub').textContent = d.subStatus || '—';
  $('kAmount').textContent = d.chequeInfo ? d.chequeInfo.amount : (d.amount ? d.amount + ' ₸' : '—');
  $('kMethod').textContent = d.chequeInfo ? (d.chequeInfo.method || '—') : '—';
  $('kTxn').textContent = d.transactionId || '—';
  renderSteps(d.status);
}

async function poll(){
  if (!current) return;
  try{
    var r = await call('/v2/status?processId=' + encodeURIComponent(current));
    show($('raw'), r.body);
    if (r.body.statusCode && r.body.statusCode !== 0){
      $('curErr').textContent = '[' + r.body.statusCode + '] ' + r.body.errorText;
      stopPoll(); return;
    }
    $('curErr').textContent = '';
    renderCurrent(r.body.data);
    if (r.body.data.status === 'wait'){
      var t = TXNS[current];
      if (t && t.hold){
        stopPoll();
        $('curErr').textContent = '交易已挂起（hold）：点「完成支付 / 完成退款」才落定。';
      }else{ timer = setTimeout(poll, 700); }
    }
    else { stopPoll(); loadList(); loadLedger(); }
  }catch(e){ $('curErr').textContent = e.message; stopPoll(); }
}
function stopPoll(){ if (timer){ clearTimeout(timer); timer = null; } }

function pendingIds(kind){
  return Object.keys(TXNS).filter(function(k){
    return TXNS[k].status === 'wait' && TXNS[k].type === kind;
  }).sort();
}

function updateDoneButtons(){
  var payIds = pendingIds('payment'), refIds = pendingIds('refund');
  // 点餐机自己发起的交易也要能被接管：没选中任何交易时自动跟随最新一笔挂起的
  if (!current && payIds.concat(refIds).length){
    var all = payIds.concat(refIds).sort();
    current = all[all.length - 1];
  }
  var pay = payIds.length, ref = refIds.length;
  $('doneHint').textContent = (pay || ref)
    ? '挂起中：支付 ' + pay + ' 笔 / 退款 ' + ref + ' 笔 —— 点按钮或调 /ui/complete 落定。'
    : '当前没有挂起的交易（点餐机发起后会立刻出现在这里）。';
  $('doneHint').style.color = (pay || ref) ? 'var(--accent)' : '';
}

async function completeCurrent(kind){
  var pid = null;
  var t = TXNS[current];
  if (t && t.status === 'wait' && t.type === kind) pid = current;
  if (!pid){  // 当前没选中就自动挑最近一笔同类挂起交易
    var ids = pendingIds(kind);
    if (ids.length) pid = ids[ids.length - 1];
  }
  if (!pid){
    $('curErr').textContent = '没有挂起的' + (kind === 'payment' ? '支付' : '退款') +
      '交易：先勾选「手动完成」并发起一笔。';
    return;
  }
  current = pid;
  try{
    var r = await call('/ui/complete?processId=' + encodeURIComponent(pid) +
                       '&outcome=' + encodeURIComponent($('doneOutcome').value));
    show($('raw'), r.body);
    if (r.body.statusCode && r.body.statusCode !== 0){
      $('curErr').textContent = '[' + r.body.statusCode + '] ' + (r.body.errorText || '') +
        (r.body.data && r.body.data.message ? ' — ' + r.body.data.message : '');
      loadList();
      return;
    }
    $('curErr').textContent = '';
    stopPoll(); await loadList(); poll(); loadLedger();
  }catch(e){ $('curErr').textContent = e.message; }
}

async function loadList(){
  try{
    var r = await call('/ui/transactions');
    var items = r.body.data || [];
    TXNS = {}; items.forEach(function(t){ TXNS[t.processId] = t; });
    updateDoneButtons();
    var tb = $('listBody');
    if (!items.length){ tb.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">暂无交易</td></tr>'; return; }
    tb.innerHTML = items.map(function(t){
      var cls = t.processId === current ? ' class="cur"' : '';
      return '<tr' + cls + ' data-pid="' + t.processId + '">' +
        '<td class="id">' + t.processId.slice(-8) + '</td>' +
        '<td>' + t.type + '</td>' +
        '<td>' + t.amount + '</td>' +
        '<td><span class="badge ' + t.status + '">' + t.status + '</span></td>' +
        '<td>' + (t.subStatus || '') + '</td></tr>';
    }).join('');
    Array.prototype.forEach.call(tb.querySelectorAll('tr[data-pid]'), function(tr){
      tr.onclick = function(){ current = tr.getAttribute('data-pid'); loadList(); poll(); };
    });
  }catch(e){ /* ignore */ }
}

async function loadLedger(){
  try{
    var r = await call('/ui/ledger');
    LEDGER = r.body.data || [];
  }catch(e){ LEDGER = []; }
}

async function loadConfig(){
  try{
    var r = await call('/ui/config');
    var d = r.body.data || {};
    $('cfgMethod').value = d.method || 'auto';
    $('cfgOutcome').value = d.outcome || 'success';
    $('cfgHold').checked = d.hold === true;
    var manual = MANUAL || d.hold === true;
    $('mode').textContent = manual ? '手动完成：需点按钮完成' : '自动完成';
    $('mode').className = 'pill ' + (manual ? 'on' : 'off');
  }catch(e){ /* ignore */ }
}

$('btnSaveCfg').onclick = async function(){
  $('cfgErr').textContent = '';
  try{
    var q = 'method=' + encodeURIComponent($('cfgMethod').value) +
            '&outcome=' + encodeURIComponent($('cfgOutcome').value) +
            '&hold=' + ($('cfgHold').checked ? '1' : '0');
    var r = await call('/ui/config?' + q);
    show($('raw'), r.body);
    if (r.body.statusCode && r.body.statusCode !== 0){
      $('cfgErr').textContent = '[' + r.body.statusCode + '] ' + (r.body.errorText || '');
      return;
    }
    $('cfgHint').textContent = '已保存：' + JSON.stringify(r.body.data) +
      ' —— 对收银机下一次发起的交易生效。';
    loadConfig();
  }catch(e){ $('cfgErr').textContent = e.message; }
};

$('btnRefresh').onclick = function(){ if (current) poll(); };
$('btnDonePay').onclick = function(){ completeCurrent('payment'); };
$('btnDoneRefund').onclick = function(){ completeCurrent('refund'); };
$('btnActualize').onclick = async function(){
  if (!current) return;
  try{
    var r = await call('/v2/actualize?processId=' + encodeURIComponent(current) +
                       '&outcome=' + encodeURIComponent($('actOutcome').value));
    show($('raw'), r.body);
    $('curErr').textContent = (r.body.statusCode && r.body.statusCode !== 0)
      ? '[' + r.body.statusCode + '] ' + (r.body.errorText || '') +
        (r.body.data && r.body.data.message ? ' — ' + r.body.data.message : '')
      : '';
    if (r.body.statusCode === 0){ poll(); }
    else if (r.body.statusCode === 100 || r.body.statusCode === 105){ loadList(); }
  }catch(e){ $('curErr').textContent = e.message; }
};

$('btnReset').onclick = async function(){
  try{
    var r = await call('/v2/reset');
    show($('raw'), r.body);
    current = null; stopPoll(); loadList(); loadLedger();
  }catch(e){ alert(e.message); }
};

var shownRequest = null, approving = false, approvingAt = 0;
var DECIDED = {};          // 点过「允许/拒绝/稍后处理」的请求 id：永不再弹，避免关了又弹
var LAST_PENDING = null;   // 最近一次拿到的待审批请求，按钮据此记账
var POLL_FAILS = 0;        // 连续轮询失败次数：长时间拿不到状态就收起，别永久挂着

function closeApproval(){
  shownRequest = null;
  $('approveModal').classList.add('hidden');
}
function maybeAskApproval(pending){
  var modal = $('approveModal');
  // 服务端没有待审批请求 —— 无条件收起，且不能被 approving 挡住
  // （否则「点允许 -> 关闭 -> 旧轮询响应后到 -> 又弹开」的时序会让它一直显示）
  if (!pending){ POLL_FAILS = 0; LAST_PENDING = null; closeApproval(); return; }
  LAST_PENDING = pending;
  if (approving && Date.now() - approvingAt < 10000) return;  // 提交中不开新弹窗
  approving = false;                                          // 卡住的提交状态自动解除
  if (DECIDED[pending.id]) return;           // 已处理过的请求不再打扰
  if (pending.expiresIn <= 0) return;        // 即将超时，不弹
  if (shownRequest === pending.id) return;   // 同一个请求只弹一次
  shownRequest = pending.id;
  $('apName').textContent = '收银软件「' + pending.name + '」请求注册（'
    + pending.requestedAt + '，请求号 ' + pending.id + '）';
  $('apHint').textContent = '来自收银软件的 /v2/register 调用，还剩约 ' +
    pending.expiresIn + ' 秒：不处理会自动失败。允许才签发 token，拒绝则本次注册失败。';
  $('apErr').textContent = '';
  modal.classList.remove('hidden');
}

async function decideRegistration(allow){
  // 先记账再发请求：无论这次调用成功与否，这个请求都不再弹出来
  if (LAST_PENDING) DECIDED[LAST_PENDING.id] = 1;
  if (approving){ closeApproval(); return; }   // 双击不算两次
  approving = true; approvingAt = Date.now();
  $('btnAllow').disabled = true; $('btnDeny').disabled = true;
  $('apErr').textContent = '';
  try{
    var r = await call('/ui/registration/' + (allow ? 'approve' : 'reject'));
    show($('raw'), r.body);
    closeApproval();                           // 点过就收起，不等下一轮轮询
    if (r.body.statusCode && r.body.statusCode !== 0){
      $('apErr').textContent = '[' + r.body.statusCode + '] ' + (r.body.errorText || '');
      await loadRegistration();
      return;
    }
    await loadRegistration();
  }catch(e){
    $('apErr').textContent = e.message;
    // 请求压根没发出去 —— 撤销售账并重新弹窗，否则用户看不到失败原因
    if (LAST_PENDING) delete DECIDED[LAST_PENDING.id];
    if (LAST_PENDING) maybeAskApproval(LAST_PENDING);
  }
  finally{
    approving = false;
    $('btnAllow').disabled = false; $('btnDeny').disabled = false;
  }
}

function dismissApproval(){
  if (LAST_PENDING) DECIDED[LAST_PENDING.id] = 1;
  closeApproval();
}

async function loadRegistration(){
  try{
    var r = await call('/ui/registration');
    POLL_FAILS = 0;
    maybeAskApproval(r.body.pending || null);
    var d = r.body.data;
    if (!d){
      $('rName').textContent = '—'; $('rDevice').textContent = '—';
      $('rToken').textContent = '—'; $('rRefresh').textContent = '—';
      $('rExp').textContent = '—'; $('rTime').textContent = '—';
      $('tokenHint').textContent = '等待收银机发起注册：GET /v2/register?name=Kashier1';
      return;
    }
    $('rName').textContent = d.deviceName || '—';
    $('rDevice').textContent = d.deviceId || '—';
    $('rToken').textContent = d.accessToken || '—';
    $('rRefresh').textContent = d.refreshToken || '—';
    $('rExp').textContent = d.expirationDate || '—';
    $('rTime').textContent = d.registeredAt || '—';
    if (d.accessToken && d.accessToken !== token){   // 跟随收银机注册结果
      token = d.accessToken;
      localStorage.setItem('kaspi_token', token);
      $('token').value = token;
    }
    $('tokenHint').textContent = d.active
      ? '已注册：token 由收银机获取，页面自动同步使用。'
      : '该 token 已失效/已撤销：让收银机重新调用 /v2/register。';
  }catch(e){
    POLL_FAILS++;
    // 连续 3 次（约 9 秒）拿不到状态就收起：否则弹窗会一直挂在那里
    if (POLL_FAILS >= 3){ LAST_PENDING = null; closeApproval(); }
  }
}

$('token').onchange = function(){
  token = this.value.trim();
  localStorage.setItem('kaspi_token', token);
};

$('btnCopy').onclick = function(){
  navigator.clipboard.writeText($('raw').textContent);
};
$('btnAllow').onclick = function(){ decideRegistration(true); };
$('btnDeny').onclick = function(){ decideRegistration(false); };
$('btnDismiss').onclick = function(){ dismissApproval(); };

if (SECURE){ $('secureCard').classList.add('secure'); }
$('mode').textContent = MANUAL ? '手动确认：需点按钮完成' : '自动完成';
$('mode').className = 'pill ' + (MANUAL ? 'on' : 'off');
$('token').value = token;
loadDevice();
loadConfig();
loadRegistration();
loadList();
loadLedger();
listTimer = setInterval(function(){ loadList(); loadRegistration(); }, 3000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
