from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import parse_lark_bitable_url, resolve_workspace_paths
from core.db import (
    connect,
    configure_lark_board,
    configure_lark_identity,
    init_workspace,
    inspect_workspace,
    resolve_lark_wiki_bitable,
    verify_lark_user_identity,
)
from core.lark_board import (
    LarkBoardClient,
    LarkRequestError,
    TASK_FIELD_SPECS,
    _board_context,
    _items,
    _record_page,
    _single_record,
    get_lark_task,
    grant_lark_board_access,
    initialize_lark_board,
    list_lark_tasks,
    upsert_lark_task,
    verify_lark_board,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeBoardClient:
    def __init__(self):
        self.table = {"table_id": "tblDefault", "table_name": "数据表", "primary_field": "fldTitle"}
        self.fields = [{"field_id": "fldTitle", "field_name": "文本", "type": "text", "is_primary": True}]
        self.views = [{"view_id": "vewGrid", "view_name": "表格", "view_type": "grid"}]
        self.records = {
            f"recBlank{index}": {"record_id": f"recBlank{index}", "fields": {"文本": None}}
            for index in range(5)
        }
        self.created_tables = 0
        self.created_fields = 0
        self.created_views = 0
        self.deleted_records = 0
        self.fail_record_read = False
        self.record_not_found_attempts = 0
        self.grouped_views = 0
        self.permission_allowed = True
        self.write_error = None
        self.granted_open_ids = []
        self.write_delay = 0
        self.active_writes = 0
        self.max_active_writes = 0
        self.write_counter_lock = threading.Lock()

    def authenticate(self):
        return None

    def check_document_permission(self, action):
        return self.permission_allowed

    def get_base(self):
        return {"base_token": "bascnTest", "name": "Test board", "url": "https://example.feishu.cn/base/bascnTest"}

    def get_bot_info(self):
        return {"open_id": "ou_bot"}

    def add_collaborator(self, open_id):
        self.granted_open_ids.append(open_id)

    def list_tables(self):
        return [dict(self.table)]

    def create_table(self, name):
        self.created_tables += 1
        self.table = {"table_id": "tblTasks", "table_name": name, "primary_field": "fldTitle"}
        self.fields = [{"field_id": "fldTitle", "field_name": "Text", "type": "text", "is_primary": True}]
        self.views = [{"view_id": "vewGrid", "view_name": "Grid", "view_type": "grid"}]
        self.records = {}
        return dict(self.table)

    def update_table(self, table_id, name):
        self.table["table_name"] = name
        return dict(self.table)

    def get_table(self, table_id):
        return {"table": dict(self.table), "fields": [dict(field) for field in self.fields], "views": [dict(view) for view in self.views]}

    def update_field(self, table_id, field_id, spec):
        field = next(field for field in self.fields if field["field_id"] == field_id)
        field.update({"field_name": spec["name"], "type": spec["type"]})
        return dict(field)

    def create_field(self, table_id, spec):
        self.created_fields += 1
        field = {"field_id": f"fld{self.created_fields}", "field_name": spec["name"], "type": spec["type"], "is_primary": False}
        self.fields.append(field)
        return dict(field)

    def list_views(self, table_id):
        return [dict(view) for view in self.views]

    def create_view(self, table_id, name):
        self.created_views += 1
        view = {"view_id": "vewBoard", "view_name": name, "view_type": "kanban"}
        self.views.append(view)
        return dict(view)

    def rename_view(self, table_id, view_id, name):
        view = next(view for view in self.views if view["view_id"] == view_id)
        view["view_name"] = name
        return dict(view)

    def set_view_group(self, table_id, view_id, field_id):
        self.grouped_views += 1

    def list_records(self, table_id, *, view_id=None, limit=100, offset=0):
        records = list(self.records.values())[offset:offset + limit]
        return {"records": [dict(record) for record in records], "has_more": False}

    def get_record(self, table_id, record_id):
        if self.record_not_found_attempts:
            self.record_not_found_attempts -= 1
            raise ValueError("not_found")
        if self.fail_record_read:
            raise ValueError("record read failed")
        return dict(self.records[record_id])

    def upsert_record(self, table_id, fields, *, record_id=None):
        with self.write_counter_lock:
            self.active_writes += 1
            self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            if self.write_delay:
                time.sleep(self.write_delay)
            if self.write_error:
                raise self.write_error
            record_id = record_id or f"rec{len(self.records) + 1}"
            record = self.records.setdefault(record_id, {"record_id": record_id, "fields": {}})
            record["fields"].update(fields)
            return {"record_id": record_id}
        finally:
            with self.write_counter_lock:
                self.active_writes -= 1

    def delete_record(self, table_id, record_id):
        del self.records[record_id]
        self.deleted_records += 1


class LarkBoardTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="lark-board-", dir=ROOT / "tmp")
        self.workspace = self.temp.name
        init_workspace(self.workspace)
        with patch("core.db.fetch_lark_app_info", return_value=("Test app", None, None)):
            configure_lark_identity(self.workspace, app_id="cli_test", app_secret="secret", domain="feishu")
        configure_lark_board(self.workspace, board_url="https://example.feishu.cn/base/bascnTest")
        self.client = FakeBoardClient()
        self.client_patch = patch("core.lark_board.LarkBoardClient", return_value=self.client)
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()
        self.temp.cleanup()

    def test_verify_initialize_and_task_round_trip(self):
        configure_lark_board(
            self.workspace,
            board_url="https://example.feishu.cn/base/bascnTest?table=tblDefault&view=vewGrid",
        )
        verified = verify_lark_board(self.workspace)
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["summary"], {"status": "verified", "total": 1, "verified": 1, "failed": 0, "pending": 0})
        self.assertFalse(verified["identities"][0]["initialized"])
        self.assertEqual(
            verified["identities"][0]["checks"],
            {"auth": "passed", "api": "passed", "collaborator": "passed", "read": "passed", "write": "passed", "cleanup": "passed"},
        )
        self.assertEqual(len(self.client.records), 5)
        self.assertEqual(inspect_workspace(self.workspace)["lark_board"]["view_id"], "vewGrid")

        initialized = initialize_lark_board(self.workspace)
        self.assertFalse(initialized["created_table"])
        self.assertTrue(initialized["reused_empty_table"])
        self.assertEqual(initialized["deleted_empty_records"], 5)
        self.assertEqual(self.client.created_tables, 0)
        self.assertEqual(self.client.deleted_records, 6)
        self.assertEqual(self.client.table["table_name"], "任务")
        self.assertEqual(self.client.fields[0]["field_name"], "任务")
        self.assertEqual(self.client.created_fields, len(TASK_FIELD_SPECS))
        self.assertEqual(self.client.created_views, 1)

        initialize_lark_board(self.workspace)
        self.assertEqual(self.client.created_tables, 0)
        self.assertEqual(self.client.created_fields, len(TASK_FIELD_SPECS))
        self.assertEqual(self.client.created_views, 1)

        created = upsert_lark_task(self.workspace, task={"title": "Implement access", "status": "Ready", "role": "tl"})
        task = created["task"]
        self.assertTrue(created["created"])
        self.assertEqual(task["title"], "Implement access")
        self.assertTrue(task["task_id"].startswith("task_"))

        updated = upsert_lark_task(self.workspace, record_id=task["record_id"], task={"status": "In Progress"})
        self.assertEqual(updated["task"]["status"], "In Progress")
        self.assertEqual(get_lark_task(self.workspace, record_id=task["record_id"])["task"]["title"], "Implement access")
        self.assertEqual(len(list_lark_tasks(self.workspace)["tasks"]), 1)

        board = inspect_workspace(self.workspace)["lark_board"]
        self.assertEqual(board["access_status"], "verified")
        self.assertEqual(board["table_id"], "tblDefault")
        self.assertEqual(board["view_id"], "vewBoard")

    def test_nonempty_default_table_is_not_repurposed(self):
        self.client.records = {"recUsed": {"record_id": "recUsed", "fields": {"文本": "Existing data"}}}

        initialized = initialize_lark_board(self.workspace)

        self.assertTrue(initialized["created_table"])
        self.assertFalse(initialized["reused_empty_table"])
        self.assertEqual(self.client.created_tables, 1)
        self.assertEqual(self.client.deleted_records, 0)
        self.assertEqual(self.client.table["table_name"], "任务")

    def test_blank_default_table_without_table_id_is_reused(self):
        initialized = initialize_lark_board(self.workspace)

        self.assertFalse(initialized["created_table"])
        self.assertTrue(initialized["reused_empty_table"])
        self.assertEqual(initialized["deleted_empty_records"], 5)

    def test_verification_record_is_cleaned_up_after_read_failure(self):
        self.client.fail_record_read = True

        verification = verify_lark_board(self.workspace)

        self.assertFalse(verification["ok"])
        self.assertEqual(verification["identities"][0]["checks"]["write"], "failed")
        self.assertEqual(verification["identities"][0]["checks"]["cleanup"], "passed")
        self.assertEqual(len(self.client.records), 5)
        self.assertEqual(self.client.deleted_records, 1)
        self.assertEqual(inspect_workspace(self.workspace)["lark_board"]["access_status"], "unavailable")

    def test_verification_retries_new_record_visibility(self):
        self.client.record_not_found_attempts = 2

        verification = verify_lark_board(self.workspace)

        self.assertTrue(verification["ok"])
        self.assertEqual(verification["identities"][0]["checks"]["write"], "passed")
        self.assertEqual(self.client.deleted_records, 1)

    def test_multiple_identity_write_probes_are_serialized(self):
        with patch("core.db.fetch_lark_app_info", return_value=("Second app", None, None)):
            configure_lark_identity(self.workspace, app_id="cli_second", app_secret="secret", domain="feishu")
        self.client.write_delay = 0.02

        verification = verify_lark_board(self.workspace)

        self.assertEqual(verification["checked"], 2)
        self.assertEqual(verification["summary"]["verified"], 2)
        self.assertEqual(self.client.max_active_writes, 1)

    def test_collaborator_failure_is_reported_per_identity(self):
        self.client.permission_allowed = False
        self.client.write_error = ValueError('API call failed: HTTP 403: {"code":91403,"msg":"you don\'t have permission"}')

        verification = verify_lark_board(self.workspace)

        result = verification["identities"][0]
        self.assertEqual(result["failure_kind"], "not_collaborator")
        self.assertEqual(result["checks"]["collaborator"], "failed")
        self.assertEqual(result["checks"]["api"], "passed")
        self.assertEqual(result["checks"]["read"], "passed")
        self.assertEqual(result["checks"]["write"], "failed")
        self.assertEqual(self.client.deleted_records, 0)

    def test_permission_probe_does_not_block_effective_read_write(self):
        self.client.permission_allowed = False

        verification = verify_lark_board(self.workspace)

        result = verification["identities"][0]
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["checks"]["collaborator"], "passed")
        self.assertEqual(result["checks"]["read"], "passed")
        self.assertEqual(result["checks"]["write"], "passed")
        self.assertEqual(result["checks"]["cleanup"], "passed")

    def test_missing_api_scope_blocks_document_checks(self):
        error = LarkRequestError(
            "missing required scope",
            code="99991672",
            missing_scopes=("bitable:app",),
        )
        with patch.object(self.client, "get_base", side_effect=error):
            verification = verify_lark_board(self.workspace)

        result = verification["identities"][0]
        self.assertEqual(result["failure_kind"], "missing_scope")
        self.assertEqual(result["checks"]["api"], "failed")
        self.assertEqual(result["checks"]["collaborator"], "blocked")
        self.assertEqual(result["missing_scopes"], ["bitable:app"])

    def test_board_access_falls_back_to_a_verified_identity(self):
        state = inspect_workspace(self.workspace)
        primary_id = state["lark_board"]["primary_identity_id"]
        with patch("core.db.fetch_lark_app_info", return_value=("Second app", None, None)):
            configure_lark_identity(self.workspace, app_id="cli_second", app_secret="secret", domain="feishu")
        state = inspect_workspace(self.workspace)
        secondary_id = next(identity["id"] for identity in state["lark_identities"] if identity["app_id"] == "cli_second")
        paths = resolve_workspace_paths(self.workspace)
        with connect(paths.db_path) as conn:
            board_id = state["lark_board"]["id"]
            conn.execute(
                "INSERT INTO lark_board_identity_access (board_id, identity_id, status) VALUES (?, ?, 'failed')",
                (board_id, primary_id),
            )
            conn.execute(
                "INSERT INTO lark_board_identity_access (board_id, identity_id, status) VALUES (?, ?, 'verified')",
                (board_id, secondary_id),
            )

        _, _, identity = _board_context(self.workspace)

        self.assertEqual(identity["id"], secondary_id)

    def test_user_identity_can_grant_bot_board_access(self):
        bot_id = inspect_workspace(self.workspace)["lark_identities"][0]["id"]
        status = {
            "appId": "cli_user",
            "identity": "user",
            "tokenStatus": "valid",
            "userName": "User",
            "userOpenId": "ou_user",
        }
        verify_lark_user_identity(self.workspace, status=status)
        state = inspect_workspace(self.workspace)
        user_id = next(identity["id"] for identity in state["lark_identities"] if identity["auth_mode"] == "user")
        verify_lark_board(self.workspace, identity_id=user_id)

        result = grant_lark_board_access(self.workspace, identity_id=bot_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["grantor_identity_id"], user_id)
        self.assertEqual(self.client.granted_open_ids, ["ou_bot"])

    def test_verified_bot_can_grant_user_board_access(self):
        bot_id = inspect_workspace(self.workspace)["lark_identities"][0]["id"]
        status = {
            "appId": "cli_user",
            "identity": "user",
            "tokenStatus": "valid",
            "userName": "User",
            "userOpenId": "ou_user",
        }
        verify_lark_user_identity(self.workspace, status=status)
        state = inspect_workspace(self.workspace)
        user_id = next(identity["id"] for identity in state["lark_identities"] if identity["auth_mode"] == "user")
        verify_lark_board(self.workspace, identity_id=bot_id)

        result = grant_lark_board_access(self.workspace, identity_id=user_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["grantor_identity_id"], bot_id)
        self.assertEqual(self.client.granted_open_ids, ["ou_user"])

    def test_wiki_url_and_flat_record_page(self):
        parsed = parse_lark_bitable_url("https://example.feishu.cn/wiki/wikTest?table=tblTest&view=vewTest")
        self.assertEqual(parsed["wiki_token"], "wikTest")
        self.assertEqual(parsed["table_id"], "tblTest")
        with patch(
            "core.db.run_lark_cli_json",
            return_value={"data": {"node": {"obj_type": "bitable", "obj_token": "bascnResolved"}}},
        ):
            self.assertEqual(
                resolve_lark_wiki_bitable({"auth_mode": "user"}, "wikTest", "https://example.feishu.cn/wiki/wikTest"),
                "bascnResolved",
            )

        page = _record_page({
            "data": [[None], ["Task"]],
            "fields": ["文本"],
            "record_id_list": ["recBlank", "recTask"],
            "has_more": False,
        })
        self.assertEqual(page["records"][0], {"record_id": "recBlank", "fields": {"文本": None}})
        self.assertEqual(page["records"][1], {"record_id": "recTask", "fields": {"文本": "Task"}})
        self.assertEqual(
            _single_record({"data": [[]], "fields": [], "record_id_list": ["recCreated"]}),
            {"record_id": "recCreated", "fields": {}},
        )
        self.assertEqual(
            _single_record({"任务": None, "Status": None}, "recRead"),
            {"record_id": "recRead", "fields": {"任务": None, "Status": None}},
        )
        for key in ("tables", "fields", "views"):
            self.assertEqual(_items({key: [{"id": key}]}), [{"id": key}])

    def test_user_client_adds_collaborator_with_user_token(self):
        board = {"base_token": "bascnUser", "base_url": "https://example.feishu.cn/base/bascnUser"}
        identity = {"auth_mode": "user", "user_open_id": "ou_user"}
        status = {"appId": "cli_user", "identity": "user", "tokenStatus": "valid", "userOpenId": "ou_user"}
        self.client_patch.stop()
        try:
            with patch("core.lark_board.run_lark_cli_json", side_effect=[status, {"data": {}}]) as lark_cli:
                LarkBoardClient(identity, board).add_collaborator("ou_target")
            self.assertEqual(
                lark_cli.call_args_list[1].args[0][0:4],
                ["drive", "permission.members", "create", "--as"],
            )
        finally:
            self.client_patch.start()

    def test_bot_client_uses_its_own_credentials(self):
        board = {"base_token": "bascnBot", "base_url": "https://example.feishu.cn/base/bascnBot"}
        identity = {"auth_mode": "bot", "app_id": "cli_bot", "app_secret": "bot-secret"}
        self.client_patch.stop()
        try:
            with patch("core.lark_board.post_json", return_value=({"tenant_access_token": "tenant-token"}, None)) as token_request:
                with patch("core.lark_board.read_json", return_value=({"code": 0, "data": {"base": {"base_token": "bascnBot"}}}, None)) as request:
                    client = LarkBoardClient(identity, board)
                    self.assertEqual(client.get_base()["base_token"], "bascnBot")
                    client.add_collaborator("ou_target")
            token_request.assert_called_once()
            self.assertEqual(request.call_args_list[0].args[0].get_header("Authorization"), "Bearer tenant-token")
            self.assertEqual(request.call_args_list[1].args[0].get_method(), "POST")
            self.assertIn("/permissions/bascnBot/members?type=bitable", request.call_args_list[1].args[0].full_url)
        finally:
            self.client_patch.start()


if __name__ == "__main__":
    unittest.main()
