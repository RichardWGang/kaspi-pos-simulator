"""Регрессионные тесты симулятора / Regression tests for the Kaspi POS simulator.

Запуск / Run:  python test_simulator.py
"""

import datetime
import json
import logging
import os
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kaspi_pos_simulator as sim

sim.configure_logging(None)
logging.getLogger("KaspiPOSSimulator").setLevel(logging.ERROR)

FAST = {"PHASE_INITIALIZE_SECONDS": 0.05, "PHASE_WAIT_USER_SECONDS": 0.1,
        "PHASE_PROCESSING_SECONDS": 0.5, "PHASE_SETTLE_SECONDS": 1.2}


class ServerTestCase(unittest.TestCase):
    secure_api = False
    refund_validation = True
    manual_completion = False
    # 默认不等控制台弹窗批准：只有 TestRegistrationApproval 才打开这一步
    register_approval = False
    register_timeout = 3.0

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sim.KaspiPOSSimulatorHandler)
        cls.httpd.secure_api = cls.secure_api
        cls.httpd.cors_enabled = False
        cls.httpd.ui_enabled = True
        cls.httpd.allow_open_reset = False
        cls.httpd.refund_validation = cls.refund_validation
        cls.httpd.manual_completion = cls.manual_completion
        cls.httpd.register_approval = cls.register_approval
        cls.httpd.register_timeout = cls.register_timeout
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        sim.transactions.clear()
        sim.ledger.clear()
        self.restore_timings()

    def get(self, path, token=None, terminal=None):
        """默认带上正确的 terminalId 头（手册 3.18）；terminal=False 表示故意不发。"""
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("accesstoken", token)
        header_terminal = sim.MOCK_DATA["terminalId"] if terminal is None else terminal
        if header_terminal:
            req.add_header("terminalid", header_terminal)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def pay(self, token=None, **params):
        params.setdefault("amount", 1000)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self.get(f"/v2/payment?{query}", token=token)

    def settled(self, pid, sleep=None):
        time.sleep(sleep if sleep is not None else 1.6)
        return self.get(f"/v2/status?processId={pid}")[1]

    def successful_payment(self, method="card", amount=1000, **extra):
        """完成一笔成功支付并返回 (processId, transactionId)。默认走加速时序。"""
        self.set_fast()
        _, body = self.pay(method=method, amount=amount, **extra)
        pid = body["data"]["processId"]
        body = self.settled(pid)
        self.assertEqual(body["data"]["status"], "success")
        return pid, body["data"]["transactionId"]

    def set_fast(self):
        for key, value in FAST.items():
            setattr(sim, key, value)

    def restore_timings(self):
        sim.PHASE_INITIALIZE_SECONDS = 1.0
        sim.PHASE_WAIT_USER_SECONDS = 2.0
        sim.PHASE_PROCESSING_SECONDS = 4.0
        sim.PHASE_SETTLE_SECONDS = 6.0
        sim.SCAN_TIMEOUT_SECONDS = 180.0
        sim.CONFIRMATION_TIMEOUT_SECONDS = 60.0
        sim.ACTUALIZE_MIN_INTERVAL_SECONDS = 10.0


class TestErrorCodeLayering(ServerTestCase):
    """手册 3.22：httpCode 与 statusCode 是两列，不得混用。"""

    def test_param_errors_use_http_400_with_business_code_999(self):
        for path, needle in (
            ("/v2/payment", "Missing param: amount"),
            ("/v2/payment?amount=abc", "Param unvalidated: amount"),
            ("/v2/payment?amount=0", "Must be greater 0"),
            ("/v2/status", "Missing param: processId"),
            ("/v2/actualize", "Missing param: processId"),
            ("/v2/register", "Missing param: name"),
            ("/v2/refund?amount=100", "Missing param: method"),
        ):
            http_status, body = self.get(path)
            self.assertEqual(http_status, 400, path)
            self.assertEqual(body["statusCode"], 999, path)
            self.assertIn(needle, body["errorText"], path)

    def test_unknown_path_uses_http_404_with_business_code_999(self):
        http_status, body = self.get("/v2/nope")
        self.assertEqual(http_status, 404)
        self.assertEqual(body["statusCode"], 999)
        self.assertEqual(body["errorText"], "Invalid Url. Please, contact developers")

    def test_business_errors_stay_on_http_200(self):
        for path, expected in (("/v2/status?processId=nope", 101),):
            http_status, body = self.get(path)
            self.assertEqual(http_status, 200, f"{path} must not use a business code as HTTP status")
            self.assertEqual(body["statusCode"], expected)
            self.assertIn("errorText", body)

    def test_concurrent_payment_returns_107_over_http_200(self):
        self.pay()
        http_status, body = self.pay(amount=2000)
        self.assertEqual(http_status, 200)
        self.assertEqual(body["statusCode"], 107)


class TestSubStatusEnum(ServerTestCase):
    """手册 3.18：subStatus 只能取 11 个枚举值。"""

    def test_fail_uses_payment_method_specific_failure(self):
        self.set_fast()
        try:
            for method, expected in (
                ("qr", "QrTransactionFailure"),
                ("card", "CardTransactionFailure"),
            ):
                self.get("/v2/reset")
                _, body = self.pay(method=method, outcome="fail")
                body = self.settled(body["data"]["processId"])
                self.assertEqual(body["data"]["status"], "fail")
                self.assertEqual(body["data"]["subStatus"], expected)
                self.assertIn(expected, sim.SUBSTATUS_ENUM)
        finally:
            self.restore_timings()

    def test_unknown_uses_process_cancelled(self):
        self.set_fast()
        try:
            _, body = self.pay(outcome="unknown")
            body = self.settled(body["data"]["processId"])
            self.assertEqual(body["data"]["status"], "unknown")
            self.assertEqual(body["data"]["subStatus"], "ProcessCancelled")
        finally:
            self.restore_timings()

    def test_refund_in_progress_uses_process_refund(self):
        self.set_fast()
        try:
            _, txn_id = self.successful_payment(method="card")
            _, body = self.get(f"/v2/refund?amount=100&method=card&transactionId={txn_id}")
            pid = body["data"]["processId"]
            time.sleep(0.4)
            body = self.get(f"/v2/status?processId={pid}")[1]
            self.assertEqual(body["data"]["subStatus"], "ProcessRefund")
        finally:
            self.restore_timings()

    def test_card_payment_walks_through_pin_code(self):
        self.set_fast()
        try:
            _, body = self.pay(method="card")
            pid = body["data"]["processId"]
            time.sleep(0.3)
            first = self.get(f"/v2/status?processId={pid}")[1]["data"]["subStatus"]
            time.sleep(0.5)
            second = self.get(f"/v2/status?processId={pid}")[1]["data"]["subStatus"]
            self.assertEqual(first, "ProcessingCard")
            self.assertEqual(second, "WaitForPinCode")
        finally:
            self.restore_timings()

    def test_observed_substatus_values_all_belong_to_manual_enum(self):
        self.set_fast()
        try:
            for outcome in ("success", "fail", "unknown"):
                for method in ("qr", "card"):
                    self.get("/v2/reset")
                    _, body = self.pay(method=method, outcome=outcome)
                    pid = body["data"]["processId"]
                    for _ in range(6):
                        body = self.get(f"/v2/status?processId={pid}")[1]
                        self.assertIn(body["data"]["subStatus"], sim.SUBSTATUS_ENUM)
                        time.sleep(0.25)
        finally:
            self.restore_timings()


class TestRefundValidation(ServerTestCase):
    """手册 2.8 / 3.3 / 3.20：退款方式一致、金额不超可用额度、transactionId 真实存在。"""

    def test_refund_with_mismatched_method_is_rejected(self):
        _, txn_id = self.successful_payment(method="card", amount=1000)
        http_status, body = self.get(
            f"/v2/refund?amount=100&method=qr&transactionId={txn_id}"
        )
        self.assertEqual(http_status, 400)
        self.assertEqual(body["statusCode"], 999)
        self.assertIn("must match the original payment method", body["errorText"])

    def test_refund_with_unknown_transaction_id_is_rejected(self):
        http_status, body = self.get(
            "/v2/refund?amount=100&method=card&transactionId=does-not-exist"
        )
        self.assertEqual(http_status, 400)
        self.assertEqual(body["statusCode"], 999)
        self.assertIn("Transaction not found", body["errorText"])

    def test_refund_above_available_amount_is_rejected(self):
        _, txn_id = self.successful_payment(method="card", amount=1000)
        http_status, body = self.get(
            f"/v2/refund?amount=1001&method=card&transactionId={txn_id}"
        )
        self.assertEqual(http_status, 400)
        self.assertEqual(body["statusCode"], 999)
        self.assertIn("exceeds available amount", body["errorText"])

    def test_partial_refunds_consume_the_available_amount(self):
        self.set_fast()
        try:
            _, txn_id = self.successful_payment(method="card", amount=1000)
            _, body = self.get(
                f"/v2/refund?amount=400&method=card&transactionId={txn_id}"
            )
            self.settled(body["data"]["processId"])
            self.assertEqual(sim.ledger[txn_id]["refunded"], 400)

            http_status, body = self.get(
                f"/v2/refund?amount=700&method=card&transactionId={txn_id}"
            )
            self.assertEqual(body["statusCode"], 999, "only 600 should remain")

            http_status, body = self.get(
                f"/v2/refund?amount=600&method=card&transactionId={txn_id}"
            )
            self.assertEqual(body["statusCode"], 0, "600 is exactly what is left")
        finally:
            self.restore_timings()

    def test_failed_refund_releases_the_reservation(self):
        self.set_fast()
        try:
            _, txn_id = self.successful_payment(method="card", amount=1000)
            _, body = self.get(
                f"/v2/refund?amount=1000&method=card&transactionId={txn_id}&outcome=fail"
            )
            self.settled(body["data"]["processId"])
            self.assertEqual(sim.ledger[txn_id]["reserved"], 0)
            self.assertEqual(sim.ledger[txn_id]["refunded"], 0)
            _, body = self.get(
                f"/v2/refund?amount=1000&method=card&transactionId={txn_id}"
            )
            self.assertEqual(body["statusCode"], 0, "reservation must be released")
        finally:
            self.restore_timings()

    def test_matching_refund_is_accepted(self):
        _, txn_id = self.successful_payment(method="qr", amount=1000)
        http_status, body = self.get(
            f"/v2/refund?amount=1000&method=qr&transactionId={txn_id}"
        )
        self.assertEqual(http_status, 200)
        self.assertEqual(body["statusCode"], 0)

    def test_ledger_only_holds_successful_payments(self):
        self.set_fast()
        try:
            _, body = self.pay(method="card", outcome="fail")
            self.settled(body["data"]["processId"])
            self.assertEqual(len(sim.ledger), 0)
        finally:
            self.restore_timings()

    def test_reset_clears_the_ledger(self):
        self.successful_payment(method="card")
        self.assertEqual(len(sim.ledger), 1)
        self.get("/v2/reset")
        self.assertEqual(len(sim.ledger), 0)


class TestRefundValidationDisabled(ServerTestCase):
    refund_validation = False

    def test_checks_are_skipped_when_disabled(self):
        http_status, body = self.get(
            "/v2/refund?amount=999999&method=qr&transactionId=whatever"
        )
        self.assertEqual(body["statusCode"], 0)


class TestTokenHandling(ServerTestCase):
    def test_expiration_date_matches_manual_format(self):
        _, body = self.get("/v2/register?name=Kashier1")
        value = body["data"]["expirationDate"]
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
                         "manual 3.15 uses YYYY-MM-DD HH:MM:SS")
        datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")

    def test_revoke_works_without_a_token_outside_secure_mode(self):
        _, reg = self.get("/v2/register?name=Kashier1")
        refresh = reg["data"]["refreshToken"]
        old = reg["data"]["accessToken"]
        http_status, body = self.get(f"/v2/revoke?name=Kashier1&refreshToken={refresh}")
        self.assertEqual(http_status, 200)
        self.assertEqual(body["statusCode"], 0, "revoke must not require accesstoken")
        self.assertNotEqual(body["data"]["accessToken"], old)
        self.assertNotIn(old, sim.api_tokens)

    def test_revoke_with_wrong_refresh_token_returns_105_with_data_message(self):
        self.get("/v2/register?name=Kashier1")
        _, body = self.get("/v2/revoke?name=Kashier1&refreshToken=wrong")
        self.assertEqual(body["statusCode"], 105)
        self.assertEqual(body["errorText"], "Error acquired in service process")
        self.assertIn("message", body["data"])

    def test_revoke_with_wrong_name_returns_105(self):
        _, reg = self.get("/v2/register?name=Kashier1")
        _, body = self.get(
            f"/v2/revoke?name=Other&refreshToken={reg['data']['refreshToken']}"
        )
        self.assertEqual(body["statusCode"], 105)

    def test_revoke_requires_refresh_token(self):
        http_status, body = self.get("/v2/revoke?name=Kashier1")
        self.assertEqual(http_status, 400)
        self.assertEqual(body["statusCode"], 999)


class TestTimeDrivenStateMachine(ServerTestCase):
    def test_late_first_poll_still_reaches_terminal_state(self):
        self.set_fast()
        try:
            _, body = self.pay()
            pid = body["data"]["processId"]
            time.sleep(2.0)
            _, body = self.get(f"/v2/status?processId={pid}")
            self.assertEqual(body["data"]["status"], "success",
                             "a poll after the settle window must return a terminal state")
        finally:
            self.restore_timings()

    def test_status_is_idempotent(self):
        self.set_fast()
        try:
            _, body = self.pay()
            pid = body["data"]["processId"]
            time.sleep(1.4)
            first = self.get(f"/v2/status?processId={pid}")[1]
            time.sleep(0.5)
            second = self.get(f"/v2/status?processId={pid}")[1]
            self.assertEqual(first["data"]["status"], second["data"]["status"])
            self.assertEqual(first["data"].get("transactionId"),
                             second["data"].get("transactionId"))
        finally:
            self.restore_timings()

    def test_payment_method_is_chosen_once(self):
        _, body = self.pay(method="")
        pid = body["data"]["processId"]
        seen = set()
        for _ in range(8):
            self.get(f"/v2/status?processId={pid}")
            method = sim.transactions[pid].get("method")
            if method:
                seen.add(method)
            time.sleep(0.35)
        self.assertLessEqual(len(seen), 1, f"payment method flipped mid-transaction: {seen}")

    def test_explicit_method_is_honoured(self):
        self.set_fast()
        try:
            _, body = self.pay(method="qr")
            body = self.settled(body["data"]["processId"])
            self.assertEqual(body["data"]["subStatus"], "QrTransactionSuccess")
            self.assertIn("orderNumber", body["data"]["chequeInfo"])
        finally:
            self.restore_timings()


class TestActualizeIsReachable(ServerTestCase):
    def test_unknown_then_actualize(self):
        self.set_fast()
        try:
            _, body = self.pay(outcome="unknown")
            pid = body["data"]["processId"]
            time.sleep(1.4)
            _, body = self.get(f"/v2/status?processId={pid}")
            self.assertEqual(body["data"]["status"], "unknown")

            _, body = self.get(f"/v2/actualize?processId={pid}")
            self.assertEqual(body["statusCode"], 0, "actualize must be reachable")
            self.assertEqual(body["data"]["status"], "success")
            self.assertIn("transactionId", body["data"])

            _, body = self.get(f"/v2/actualize?processId={pid}")
            self.assertEqual(body["statusCode"], 103, "re-actualize must be rejected")
        finally:
            self.restore_timings()

    def test_actualize_rejects_non_unknown_transaction(self):
        _, body = self.pay()
        pid = body["data"]["processId"]
        _, body = self.get(f"/v2/actualize?processId={pid}")
        self.assertEqual(body["statusCode"], 103)

    def test_failed_transaction(self):
        self.set_fast()
        try:
            _, body = self.pay(outcome="fail")
            body = self.settled(body["data"]["processId"])
            self.assertEqual(body["data"]["status"], "fail")
            self.assertIn("message", body["data"])
        finally:
            self.restore_timings()


class TestLifecycle(ServerTestCase):
    def test_completed_time_is_not_refreshed_on_poll(self):
        self.set_fast()
        try:
            _, body = self.pay()
            pid = body["data"]["processId"]
            time.sleep(1.4)
            self.get(f"/v2/status?processId={pid}")
            first = sim.transactions[pid]["completedTime"]
            time.sleep(0.6)
            self.get(f"/v2/status?processId={pid}")
            second = sim.transactions[pid]["completedTime"]
            self.assertEqual(first, second,
                             "polling must not refresh completedTime (leaks memory)")
        finally:
            self.restore_timings()

    def test_process_ids_are_unique(self):
        ids = set()
        for _ in range(25):
            _, body = self.pay()
            ids.add(body["data"]["processId"])
            self.get("/v2/reset")
        self.assertEqual(len(ids), 25, "processId collision detected")

    def test_refund_requires_original_transaction_id(self):
        _, body = self.get("/v2/refund?amount=100&method=card")
        self.assertEqual(body["statusCode"], 999)

    def test_refund_amount_is_negative_on_cheque(self):
        self.set_fast()
        try:
            _, txn_id = self.successful_payment(method="card", amount=5000)
            _, body = self.get(f"/v2/refund?amount=500&method=card&transactionId={txn_id}")
            body = self.settled(body["data"]["processId"])
            self.assertEqual(body["data"]["status"], "success")
            self.assertEqual(body["data"]["chequeInfo"]["amount"], "- 500 ₸")
            self.assertEqual(body["data"]["chequeInfo"]["type"], "refund")
        finally:
            self.restore_timings()

    def test_transactions_are_kept_for_24_hours(self):
        self.assertEqual(sim.TERMINAL_RETENTION_SECONDS, 86400,
                         "manual: completed transactions are kept for 24 hours")

    def test_device_info(self):
        _, body = self.get("/v2/deviceinfo")
        self.assertEqual(body["statusCode"], 0)
        self.assertEqual(body["data"]["terminalId"], sim.MOCK_DATA["terminalId"])

    def test_reset(self):
        self.pay()
        _, body = self.get("/v2/reset")
        self.assertEqual(body["statusCode"], 0)
        self.assertEqual(len(sim.transactions), 0)


class TestWebConsole(ServerTestCase):
    def test_ui_is_served(self):
        req = urllib.request.Request(self.base + "/ui")
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8")
            self.assertEqual(resp.headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("Kaspi POS", html)
        self.assertNotIn("__SECURE_API__", html, "placeholder must be substituted")

    def test_console_sends_the_terminal_id_header(self):
        req = urllib.request.Request(self.base + "/ui")
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("terminalid", html, "console must send terminalId (manual 3.18)")
        self.assertIn("alaqan", html)

    def test_transactions_endpoint(self):
        self.pay()
        _, body = self.get("/ui/transactions")
        self.assertEqual(body["statusCode"], 0)
        self.assertEqual(len(body["data"]), 1)
        self.assertIn("createTime", body["data"][0])

    def test_ledger_endpoint_lists_refundable_payments(self):
        self.set_fast()
        try:
            self.successful_payment(method="card", amount=3000)
            _, body = self.get("/ui/ledger")
            self.assertEqual(body["statusCode"], 0)
            self.assertEqual(len(body["data"]), 1)
            self.assertEqual(body["data"][0]["available"], 3000)
        finally:
            self.restore_timings()


class TestTerminalIdHeader(ServerTestCase):
    """手册 3.18：/v2/status 的请求头同时要求 accessToken 与 terminalId。"""

    def test_status_without_terminal_id_is_rejected(self):
        _, body = self.pay()
        pid = body["data"]["processId"]
        http_status, body = self.get(f"/v2/status?processId={pid}", terminal=False)
        self.assertEqual(http_status, 400)
        self.assertEqual(body["statusCode"], 999)
        self.assertIn("terminalId", body["errorText"])

    def test_status_with_wrong_terminal_id_is_rejected(self):
        _, body = self.pay()
        pid = body["data"]["processId"]
        http_status, body = self.get(f"/v2/status?processId={pid}", terminal="00000000")
        self.assertEqual(http_status, 400)
        self.assertEqual(body["statusCode"], 999)
        self.assertIn("Unknown terminal", body["errorText"])

    def test_status_with_matching_terminal_id_is_accepted(self):
        _, body = self.pay()
        pid = body["data"]["processId"]
        http_status, body = self.get(f"/v2/status?processId={pid}")
        self.assertEqual(http_status, 200)
        self.assertEqual(body["statusCode"], 0)

    def test_other_endpoints_do_not_need_the_terminal_header(self):
        http_status, body = self.get("/v2/deviceinfo", terminal=False)
        self.assertEqual(http_status, 200)
        self.assertEqual(body["statusCode"], 0)


class TestRemains(ServerTestCase):
    """/v2/remains：手册仅在错误码 107/108 与 3.19 中提及，此处按「退余款」实现。"""

    def test_remains_refunds_the_leftover_amount(self):
        self.set_fast()
        try:
            _, txn_id = self.successful_payment(method="card", amount=1000)
            _, body = self.get(
                f"/v2/refund?amount=400&method=card&transactionId={txn_id}"
            )
            self.settled(body["data"]["processId"])

            _, body = self.get(f"/v2/remains?transactionId={txn_id}")
            self.assertEqual(body["statusCode"], 0)
            pid = body["data"]["processId"]
            self.assertEqual(sim.transactions[pid]["amount"], 600, "1000 - 400 = 600")
            body = self.settled(pid)
            self.assertEqual(body["data"]["status"], "success")
            self.assertEqual(sim.ledger[txn_id]["refunded"], 1000)
        finally:
            self.restore_timings()

    def test_remains_needs_a_known_transaction(self):
        http_status, body = self.get("/v2/remains?transactionId=nope")
        self.assertEqual(http_status, 400)
        self.assertEqual(body["statusCode"], 999)
        self.assertIn("Transaction not found", body["errorText"])

    def test_remains_with_nothing_left_is_rejected(self):
        self.set_fast()
        try:
            _, txn_id = self.successful_payment(method="card", amount=500)
            _, body = self.get(f"/v2/refund?amount=500&method=card&transactionId={txn_id}")
            self.settled(body["data"]["processId"])
            http_status, body = self.get(f"/v2/remains?transactionId={txn_id}")
            self.assertEqual(http_status, 400)
            self.assertIn("Nothing left to refund", body["errorText"])
        finally:
            self.restore_timings()


class TestActualizeFrequencyAndOutcomes(ServerTestCase):
    """手册 3.19：最大频率每 10 秒一次；仍可能返回 unknown。"""

    def _unknown_transaction(self):
        self.set_fast()
        _, body = self.pay(outcome="unknown")
        pid = body["data"]["processId"]
        time.sleep(1.4)
        body = self.get(f"/v2/status?processId={pid}")[1]
        self.assertEqual(body["data"]["status"], "unknown")
        return pid

    def test_repeated_actualize_is_throttled(self):
        try:
            pid = self._unknown_transaction()
            _, body = self.get(f"/v2/actualize?processId={pid}&outcome=unknown")
            self.assertEqual(body["statusCode"], 0)
            self.assertEqual(body["data"]["status"], "unknown")

            http_status, body = self.get(f"/v2/actualize?processId={pid}")
            self.assertEqual(http_status, 200)
            self.assertEqual(body["statusCode"], 105)
            self.assertIn("10 seconds", body["data"]["message"])
        finally:
            self.restore_timings()

    def test_actualize_works_again_after_the_throttle_window(self):
        sim.ACTUALIZE_MIN_INTERVAL_SECONDS = 0.0
        try:
            pid = self._unknown_transaction()
            self.get(f"/v2/actualize?processId={pid}&outcome=unknown")
            _, body = self.get(f"/v2/actualize?processId={pid}&outcome=success")
            self.assertEqual(body["statusCode"], 0)
            self.assertEqual(body["data"]["status"], "success")
        finally:
            self.restore_timings()

    def test_actualize_can_stay_unknown(self):
        try:
            pid = self._unknown_transaction()
            _, body = self.get(f"/v2/actualize?processId={pid}&outcome=unknown")
            self.assertEqual(body["data"]["status"], "unknown")
            self.assertEqual(body["data"]["subStatus"], "ProcessCancelled")
            _, body = self.get(f"/v2/status?processId={pid}")
            self.assertEqual(body["data"]["status"], "unknown")
        finally:
            self.restore_timings()

    def test_actualize_can_fail(self):
        try:
            pid = self._unknown_transaction()
            _, body = self.get(f"/v2/actualize?processId={pid}&outcome=fail")
            self.assertEqual(body["data"]["status"], "fail")
            self.assertIn("message", body["data"])
        finally:
            self.restore_timings()

    def test_actualize_no_internet_returns_100(self):
        try:
            pid = self._unknown_transaction()
            http_status, body = self.get(
                f"/v2/actualize?processId={pid}&outcome=nointernet"
            )
            self.assertEqual(http_status, 200, "manual 3.22 lists httpCode 200")
            self.assertEqual(body["statusCode"], 100)
            self.assertEqual(body["errorText"], "No internet")
            _, body = self.get(f"/v2/status?processId={pid}")
            self.assertEqual(body["data"]["status"], "unknown", "must stay unknown")
        finally:
            self.restore_timings()


class TestAlaqan(ServerTestCase):
    """手册 3.17 / 3.20：新增刷掌支付 alaqan，请求与响应参数与 qr/card 相同。"""

    def test_alaqan_payment_uses_an_order_number_cheque(self):
        self.set_fast()
        try:
            _, body = self.pay(method="alaqan")
            body = self.settled(body["data"]["processId"])
            self.assertEqual(body["data"]["status"], "success")
            self.assertEqual(body["data"]["chequeInfo"]["method"], "alaqan")
            self.assertIn("orderNumber", body["data"]["chequeInfo"])
            self.assertEqual(body["data"]["subStatus"], "QrTransactionSuccess")
        finally:
            self.restore_timings()

    def test_alaqan_failure_uses_a_manual_substatus(self):
        self.set_fast()
        try:
            _, body = self.pay(method="alaqan", outcome="fail")
            body = self.settled(body["data"]["processId"])
            self.assertEqual(body["data"]["subStatus"], "QrTransactionFailure")
            self.assertIn(body["data"]["subStatus"], sim.SUBSTATUS_ENUM)
        finally:
            self.restore_timings()

    def test_alaqan_refund_matches_the_original_method(self):
        _, txn_id = self.successful_payment(method="alaqan", amount=800)
        _, body = self.get(
            f"/v2/refund?amount=800&method=alaqan&transactionId={txn_id}"
        )
        self.assertEqual(body["statusCode"], 0)


class TestInitializeSubStatus(ServerTestCase):
    """手册 3.18：状态查询示例中的 subStatus 可以是 Initialize。"""

    def test_first_poll_returns_initialize(self):
        _, body = self.pay()
        pid = body["data"]["processId"]
        body = self.get(f"/v2/status?processId={pid}")[1]
        self.assertEqual(body["data"]["subStatus"], "Initialize")

    def test_initialize_is_part_of_the_manual_enum(self):
        self.assertIn("Initialize", sim.SUBSTATUS_ENUM)


class TestManualTimeouts(ServerTestCase):
    """手册 3.18：扫码超时 180 秒，支付确认超时 60 秒。"""

    def test_defaults_match_the_manual(self):
        self.assertEqual(sim.SCAN_TIMEOUT_SECONDS, 180)
        self.assertEqual(sim.CONFIRMATION_TIMEOUT_SECONDS, 60)

    def test_scan_timeout_fails_the_transaction(self):
        sim.PHASE_INITIALIZE_SECONDS = 0.0
        sim.PHASE_WAIT_USER_SECONDS = 1.0
        sim.PHASE_PROCESSING_SECONDS = 2.0
        sim.PHASE_SETTLE_SECONDS = 10 ** 6
        sim.SCAN_TIMEOUT_SECONDS = 0.3
        sim.CONFIRMATION_TIMEOUT_SECONDS = 10 ** 6
        try:
            _, body = self.pay(method="qr")
            pid = body["data"]["processId"]
            time.sleep(0.6)
            body = self.get(f"/v2/status?processId={pid}")[1]
            self.assertEqual(body["data"]["status"], "fail")
            self.assertEqual(body["data"]["subStatus"], "QrTransactionFailure")
            self.assertIn("message", body["data"])
        finally:
            self.restore_timings()

    def test_confirmation_timeout_fails_the_transaction(self):
        sim.PHASE_INITIALIZE_SECONDS = 0.0
        sim.PHASE_WAIT_USER_SECONDS = 0.0
        sim.PHASE_PROCESSING_SECONDS = 0.2
        sim.PHASE_SETTLE_SECONDS = 10 ** 6
        sim.SCAN_TIMEOUT_SECONDS = 10 ** 6
        sim.CONFIRMATION_TIMEOUT_SECONDS = 0.4
        try:
            _, body = self.pay(method="card")
            pid = body["data"]["processId"]
            time.sleep(0.1)  # 客户刷卡，进入确认阶段
            body = self.get(f"/v2/status?processId={pid}")[1]
            self.assertEqual(body["data"]["status"], "wait")
            time.sleep(0.6)
            body = self.get(f"/v2/status?processId={pid}")[1]
            self.assertEqual(body["data"]["status"], "fail")
            self.assertEqual(body["data"]["subStatus"], "CardTransactionFailure")
        finally:
            self.restore_timings()


class TestManualCompletionMode(ServerTestCase):
    """--manual-completion：点餐机的普通请求也挂起，必须点按钮才落定。"""

    manual_completion = True

    def setUp(self):
        super().setUp()
        # 自动完成的时间线压到约 1 秒，才能证明它确实「没有」自动完成
        self.set_fast()

    def test_plain_request_from_a_till_is_held(self):
        _, body = self.pay(amount=1000)          # 不带任何参数
        pid = body["data"]["processId"]
        time.sleep(2.5)
        self.assertEqual(self.get(f"/v2/status?processId={pid}")[1]["data"]["status"], "wait")
        _, body = self.get(f"/ui/complete?processId={pid}&outcome=success")
        self.assertEqual(body["data"]["status"], "success")

    def test_complete_supports_all_three_outcomes(self):
        for outcome in ("success", "fail", "unknown"):
            _, body = self.pay(amount=1000)
            pid = body["data"]["processId"]
            _, body = self.get(f"/ui/complete?processId={pid}&outcome={outcome}")
            self.assertEqual(body["data"]["status"], outcome)

    def test_completing_twice_is_refused(self):
        _, body = self.pay(amount=1000)
        pid = body["data"]["processId"]
        self.get(f"/ui/complete?processId={pid}&outcome=success")
        _, body = self.get(f"/ui/complete?processId={pid}&outcome=success")
        self.assertEqual(body["statusCode"], 105)
        self.assertTrue(body["data"]["message"])

    def test_unknown_process_id_returns_101(self):
        _, body = self.get("/ui/complete?processId=nope&outcome=success")
        self.assertEqual(body["statusCode"], 101)

    def test_hold_zero_overrides_the_server_mode(self):
        _, body = self.pay(amount=1000, hold=0)
        pid = body["data"]["processId"]
        self.assertEqual(self.settled(pid)["data"]["status"], "success")

    def test_refund_is_held_as_well(self):
        pid, txn = self.successful_payment(method="card", amount=1000, hold=0)
        _, body = self.get(f"/v2/refund?amount=500&method=card&transactionId={txn}")
        refund_id = body["data"]["processId"]
        time.sleep(2.5)
        self.assertEqual(
            self.get(f"/v2/status?processId={refund_id}")[1]["data"]["status"], "wait"
        )
        _, body = self.get(f"/ui/complete?processId={refund_id}&outcome=success")
        self.assertEqual(body["data"]["status"], "success")


class TestRegistrationInfo(ServerTestCase):
    """注册由收银机发起：/v2/register 签发 token，/ui 只读展示。"""

    def test_register_answers_with_device_name_and_id(self):
        _, body = self.get("/v2/register?name=Kashier1")
        self.assertEqual(body["statusCode"], 0)
        self.assertEqual(body["data"]["deviceName"], "Kashier1")
        self.assertTrue(body["data"]["accessToken"])
        self.assertTrue(body["data"]["refreshToken"])
        self.assertTrue(body["data"]["expirationDate"])

    def test_device_id_matches_the_terminal_id_from_deviceinfo(self):
        _, device = self.get("/v2/deviceinfo")
        _, reg = self.get("/v2/register?name=Kashier1")
        # 设备ID 就是终端号：控制台顶部 terminal: 与注册卡片显示的是同一个值
        self.assertEqual(reg["data"]["deviceId"], device["data"]["terminalId"])
        self.assertEqual(reg["data"]["deviceId"], sim.MOCK_DATA["terminalId"])

    def test_ui_mirrors_the_last_registration(self):
        _, reg = self.get("/v2/register?name=Till-01")
        _, body = self.get("/ui/registration")
        self.assertEqual(body["statusCode"], 0)
        self.assertEqual(body["data"]["deviceName"], "Till-01")
        self.assertEqual(body["data"]["deviceId"], reg["data"]["deviceId"])
        self.assertEqual(body["data"]["accessToken"], reg["data"]["accessToken"])
        self.assertEqual(body["data"]["refreshToken"], reg["data"]["refreshToken"])
        self.assertTrue(body["data"]["active"])

    def test_ui_shows_nothing_before_the_till_registers(self):
        saved = sim.last_registration
        sim.last_registration = None
        try:
            _, body = self.get("/ui/registration")
            self.assertIsNone(body["data"])
        finally:
            sim.last_registration = saved

    def test_revoked_registration_is_no_longer_active(self):
        _, reg = self.get("/v2/register?name=Kashier1")
        _, body = self.get(
            f"/v2/revoke?name=Kashier1&refreshToken={reg['data']['refreshToken']}"
        )
        self.assertEqual(body["statusCode"], 0)
        _, body = self.get("/ui/registration")
        self.assertFalse(body["data"]["active"])


class TestRegistrationApproval(ServerTestCase):
    """注册要先过控制台弹窗：请求挂起 → 允许/拒绝 → 才给结果。"""

    register_approval = True

    def setUp(self):
        super().setUp()
        self.saved_timeout = self.httpd.register_timeout
        sim.pending_registration = None

    def tearDown(self):
        self.httpd.register_timeout = self.saved_timeout
        sim.pending_registration = None

    def start_register(self, name="Kashier1"):
        """在后台发起注册（它会一直挂到有人批准为止）。"""
        box = {}

        def call():
            box["status"], box["body"] = self.get(f"/v2/register?name={name}")

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        return thread, box

    def wait_pending(self):
        for _ in range(60):
            _, body = self.get("/ui/registration")
            if body.get("pending"):
                return body["pending"]
            time.sleep(0.05)
        self.fail("the registration request never showed up in /ui")

    def test_register_waits_for_the_allow_button(self):
        thread, box = self.start_register()
        pending = self.wait_pending()
        self.assertEqual(pending["name"], "Kashier1")
        self.assertNotIn("status", box)          # 还没批准，收银机那边没有响应
        _, body = self.get("/ui/registration/approve")
        self.assertEqual(body["statusCode"], 0)
        thread.join(10)
        self.assertEqual(box["body"]["data"]["deviceName"], "Kashier1")
        self.assertTrue(box["body"]["data"]["accessToken"])
        _, body = self.get("/ui/registration")
        self.assertIsNone(body.get("pending"))

    def test_reject_makes_the_registration_fail(self):
        thread, box = self.start_register()
        self.wait_pending()
        _, body = self.get("/ui/registration/reject")
        self.assertEqual(body["statusCode"], 0)
        thread.join(10)
        self.assertEqual(box["status"], 400)
        self.assertEqual(box["body"]["statusCode"], 999)
        self.assertNotIn("accessToken", box["body"].get("data", {}))

    def test_nobody_answers_and_the_request_times_out(self):
        self.httpd.register_timeout = 0.5
        thread, box = self.start_register()
        thread.join(10)
        self.assertEqual(box["body"]["statusCode"], 105)
        _, body = self.get("/ui/registration")
        self.assertIsNone(body.get("pending"))

    def test_a_second_request_is_refused_while_one_is_pending(self):
        first, _ = self.start_register()
        self.wait_pending()
        _, body = self.get("/v2/register?name=Kashier2")
        self.assertEqual(body["statusCode"], 107)
        self.get("/ui/registration/approve")
        first.join(10)

    def test_approving_without_a_pending_request_is_an_error(self):
        status, body = self.get("/ui/registration/approve")
        self.assertEqual(status, 400)
        self.assertEqual(body["statusCode"], 999)


class TestTradeConfig(ServerTestCase):
    """交易配置：收银机不带 method / outcome / hold 时按 /ui/config 的默认值跑。"""

    def setUp(self):
        super().setUp()
        self.saved = dict(sim.trade_config)

    def tearDown(self):
        sim.trade_config.clear()
        sim.trade_config.update(self.saved)

    def transaction_row(self, process_id):
        _, body = self.get("/ui/transactions")
        return [t for t in body["data"] if t["processId"] == process_id][0]

    def test_config_is_readable_and_starts_from_the_defaults(self):
        _, body = self.get("/ui/config")
        self.assertEqual(body["statusCode"], 0)
        self.assertEqual(body["data"]["outcome"], "success")
        self.assertIsNone(body["data"]["method"])
        self.assertIsNone(body["data"]["hold"])

    def test_config_supplies_method_and_outcome(self):
        _, body = self.get("/ui/config?method=card&outcome=fail")
        self.assertEqual(body["data"]["method"], "card")
        self.assertEqual(body["data"]["outcome"], "fail")
        self.set_fast()
        _, body = self.pay(amount=1000)          # 收银机没带 method / outcome
        pid = body["data"]["processId"]
        self.assertEqual(self.settled(pid)["data"]["status"], "fail")
        self.assertEqual(self.transaction_row(pid)["method"], "card")

    def test_config_can_hold_the_till_transaction(self):
        self.get("/ui/config?hold=1")
        _, body = self.pay(amount=1000)          # 收银机没带 hold
        pid = body["data"]["processId"]
        time.sleep(3)
        self.assertEqual(
            self.get(f"/v2/status?processId={pid}")[1]["data"]["status"], "wait"
        )
        _, body = self.get(f"/ui/complete?processId={pid}&outcome=success")
        self.assertEqual(body["data"]["status"], "success")

    def test_request_parameters_win_over_the_config(self):
        self.get("/ui/config?outcome=fail")
        self.set_fast()
        _, body = self.pay(amount=1000, outcome="success")
        pid = body["data"]["processId"]
        self.assertEqual(self.settled(pid)["data"]["status"], "success")

    def test_invalid_config_values_are_rejected(self):
        status, body = self.get("/ui/config?outcome=maybe")
        self.assertEqual(status, 400)
        self.assertEqual(body["statusCode"], 999)
        status, body = self.get("/ui/config?method=cash")
        self.assertEqual(status, 400)
        self.assertEqual(body["statusCode"], 999)


class TestHttpsSupport(ServerTestCase):
    """手册 2.5 / 3.1：集成必须通过 HTTPS。"""

    def test_self_signed_certificate_can_be_generated(self):
        if not sim.find_openssl():
            self.skipTest("openssl not available on this machine")
        with tempfile.TemporaryDirectory() as tmp:
            cert = os.path.join(tmp, "cert.pem")
            key = os.path.join(tmp, "key.pem")
            self.assertEqual(sim.ensure_self_signed_cert(cert, key), cert)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=cert, keyfile=key)

    def test_cli_exposes_http_and_timeout_switches(self):
        args = sim.build_parser().parse_args(["--http", "--scan-timeout", "30"])
        self.assertTrue(args.http)
        self.assertEqual(args.scan_timeout, 30)
        args = sim.build_parser().parse_args(["--no-terminal-check", "--fast"])
        self.assertTrue(args.no_terminal_check)
        self.assertTrue(args.fast)


class TestSecureMode(ServerTestCase):
    secure_api = True

    def setUp(self):
        super().setUp()
        sim.api_tokens.clear()
        _, body = self.get("/v2/register?name=Kashier1")
        self.token = body["data"]["accessToken"]

    def test_unauthenticated_request_is_401_with_business_code_999(self):
        http_status, body = self.get("/v2/deviceinfo")
        self.assertEqual(http_status, 401)
        self.assertEqual(body["statusCode"], 999)
        self.assertEqual(body["errorText"], "Authorization failed")

    def test_token_from_header_works(self):
        http_status, body = self.get("/v2/deviceinfo", token=self.token)
        self.assertEqual(http_status, 200)
        self.assertEqual(body["statusCode"], 0)

    def test_expired_token_returns_403_with_session_message(self):
        with sim._state_lock:
            sim.api_tokens[self.token]["expiration"] = (
                datetime.datetime.now() - datetime.timedelta(hours=1)
            )
        http_status, body = self.get("/v2/deviceinfo", token=self.token)
        self.assertEqual(http_status, 403, "manual 3.15: expired token -> 403 Forbidden")
        self.assertEqual(body["statusCode"], 999)
        self.assertEqual(body["errorText"], "Session expires and need to be updated")

    def test_reset_requires_token_in_secure_mode(self):
        http_status, _ = self.get("/v2/reset")
        self.assertEqual(http_status, 401)
        http_status, body = self.get("/v2/reset", token=self.token)
        self.assertEqual(http_status, 200)

    def test_reset_is_not_locked_behind_107(self):
        self.pay(token=self.token)
        http_status, body = self.get("/v2/reset", token=self.token)
        self.assertEqual(http_status, 200)
        self.assertEqual(body["statusCode"], 0)

    def test_revoke_rotates_token(self):
        _, reg = self.get("/v2/register?name=Kashier1")
        refresh = reg["data"]["refreshToken"]
        old = reg["data"]["accessToken"]
        _, body = self.get(f"/v2/revoke?name=Kashier1&refreshToken={refresh}", token=old)
        self.assertEqual(body["statusCode"], 0)
        self.assertNotEqual(body["data"]["accessToken"], old)
        self.assertNotIn(old, sim.api_tokens)

    def test_revoke_without_token_is_rejected_in_secure_mode(self):
        _, reg = self.get("/v2/register?name=Kashier1")
        http_status, body = self.get(
            f"/v2/revoke?name=Kashier1&refreshToken={reg['data']['refreshToken']}"
        )
        self.assertEqual(http_status, 401)

    def test_token_is_masked_in_logs(self):
        self.assertNotEqual(sim.mask_secret(self.token), self.token)
        self.assertIn("...", sim.mask_secret(self.token))


if __name__ == "__main__":
    unittest.main(verbosity=2)
