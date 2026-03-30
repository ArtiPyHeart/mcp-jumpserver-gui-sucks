import asyncio
import unittest
from pathlib import Path
from argparse import Namespace
from unittest.mock import AsyncMock, Mock, patch

from mcp_jumpserver_gui_sucks import cli, service
from mcp_jumpserver_gui_sucks.auth_state import AuthState, CookieState
from mcp_jumpserver_gui_sucks.config import Settings
from mcp_jumpserver_gui_sucks.koko import KoKoProbeError
from mcp_jumpserver_gui_sucks.terminal_manager import TerminalSessionManager


class TerminalAccountResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_connection_token_payload_resolves_account_reference(self) -> None:
        resolved_target = {
            "asset": {"id": "asset-uuid", "name": "test-asset"},
            "account": {"id": "account-uuid", "username": "root"},
        }
        client_instance = Mock()
        client_instance.create_connection_token = AsyncMock(
            return_value={
                "id": "token-uuid",
                "asset": "asset-uuid",
                "account": "account-uuid",
                "connect_method": "web_cli",
                "protocol": "ssh",
            }
        )

        with (
            patch.object(
                service,
                "resolve_terminal_tool_target",
                AsyncMock(return_value=("asset-uuid", "account-uuid", resolved_target)),
            ),
            patch.object(
                service,
                "require_auth_state",
                return_value=(Mock(), Mock(), Mock()),
            ),
            patch.object(service, "JumpServerClient", return_value=client_instance),
        ):
            payload = await service.create_connection_token_payload(
                asset_id="192.168.15.70-test-data-01",
                account="root",
            )

        client_instance.create_connection_token.assert_awaited_once_with(
            asset_id="asset-uuid",
            account="account-uuid",
            protocol="ssh",
            connect_method="web_cli",
            is_reusable=False,
        )
        self.assertEqual(payload["resolved_target"], resolved_target)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["token"]["account"], "account-uuid")

    async def test_acquire_terminal_session_payload_resolves_account_reference(self) -> None:
        resolved_target = {
            "asset": {"id": "asset-uuid", "name": "test-asset"},
            "account": {"id": "account-uuid", "username": "root"},
        }

        with (
            patch.object(
                service,
                "resolve_terminal_tool_target",
                AsyncMock(return_value=("asset-uuid", "account-uuid", resolved_target)),
            ),
            patch.object(
                service,
                "ensure_terminal_auth_state",
                AsyncMock(return_value=(Mock(), Mock(), Mock(), {"cookie_session_authenticated": True})),
            ),
            patch.object(
                service.get_terminal_session_manager(),
                "open_session",
                AsyncMock(return_value={"session_handle": "session-1", "opened": True}),
            ) as open_mock,
        ):
            payload = await service.acquire_terminal_session_payload(
                asset_ref="88fa41cf-c845-4efa-9b4b-534923b5a507",
                account_ref="test-root",
            )

        open_mock.assert_awaited_once()
        _, kwargs = open_mock.await_args
        self.assertEqual(kwargs["asset_id"], "asset-uuid")
        self.assertEqual(kwargs["account"], "account-uuid")
        self.assertEqual(payload["resolved_target"], resolved_target)
        self.assertTrue(payload["terminal_auth"]["cookie_session_authenticated"])

    async def test_run_terminal_command_payload_delegates_to_manager(self) -> None:
        manager = service.get_terminal_session_manager()
        settings = Mock()

        with (
            patch.object(service, "Settings") as settings_cls,
            patch.object(manager, "prepare", AsyncMock()) as prepare_mock,
            patch.object(
                manager,
                "run_command",
                AsyncMock(return_value={"command_completed": True, "exit_status": 0}),
            ) as run_mock,
        ):
            settings_cls.from_env.return_value = settings
            payload = await service.run_terminal_command_payload(
                session_handle="session-1",
                command="hostname",
            )

        prepare_mock.assert_awaited_once_with(settings)
        run_mock.assert_awaited_once_with(
            "session-1",
            command="hostname",
            settle_timeout_seconds=1.5,
            total_timeout_seconds=20.0,
        )
        self.assertEqual(payload["exit_status"], 0)


class TerminalCliResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_terminal_target_args_keeps_resolving_even_with_asset_id_and_account(self) -> None:
        resolved_target = {
            "asset": {"id": "asset-uuid"},
            "account": {"id": "account-uuid"},
        }
        args = Namespace(
            asset_id="88fa41cf-c845-4efa-9b4b-534923b5a507",
            asset_ref="",
            account="root",
            account_ref="",
            protocol="ssh",
        )

        with patch.object(
            cli,
            "resolve_terminal_target_payload",
            AsyncMock(return_value=resolved_target),
        ) as resolve_mock:
            asset_id, account_id, resolved = await cli.resolve_terminal_target_args(args)

        resolve_mock.assert_awaited_once_with(
            asset_ref="88fa41cf-c845-4efa-9b4b-534923b5a507",
            account_ref="root",
            protocol="ssh",
        )
        self.assertEqual(asset_id, "asset-uuid")
        self.assertEqual(account_id, "account-uuid")
        self.assertEqual(resolved, resolved_target)


class TerminalCliExecTests(unittest.TestCase):
    def test_terminal_exec_command_uses_a_single_event_loop(self) -> None:
        args = Namespace(
            asset_id="",
            asset_ref="192.168.15.70-test-data-01",
            account="",
            account_ref="test-root",
            remote_command="hostname",
            protocol="ssh",
            connect_method="ssh",
            cols=120,
            rows=32,
            startup_idle_timeout_seconds=1.5,
            command_idle_timeout_seconds=1.5,
            total_timeout_seconds=20.0,
        )
        resolved_target = {
            "asset": {"id": "asset-uuid"},
            "account": {"id": "account-uuid"},
        }
        real_asyncio_run = asyncio.run
        run_calls = 0

        def tracking_run(coro):
            nonlocal run_calls
            run_calls += 1
            return real_asyncio_run(coro)

        with (
            patch.object(
                cli,
                "resolve_terminal_target_args",
                AsyncMock(return_value=("asset-uuid", "account-uuid", resolved_target)),
            ),
            patch.object(
                cli,
                "acquire_terminal_session_payload",
                AsyncMock(return_value={"session_handle": "session-1"}),
            ),
            patch.object(
                cli,
                "run_terminal_command_payload",
                AsyncMock(return_value={"command_completed": True, "exit_status": 0}),
            ),
            patch.object(
                cli,
                "close_terminal_session_payload",
                AsyncMock(),
            ) as close_mock,
            patch.object(cli, "print_json") as print_json_mock,
            patch.object(cli.asyncio, "run", side_effect=tracking_run),
        ):
            exit_code = cli.terminal_exec_command(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_calls, 1)
        close_mock.assert_awaited_once_with("session-1")
        print_json_mock.assert_called_once()


class TerminalManagerTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_session_releases_opening_slot_after_timeout(self) -> None:
        settings = Settings(
            base_url="https://jumpserver.example.com",
            org_id="",
            state_dir=Path("/tmp"),
            state_file=Path("/tmp/jumpserver-auth-state.json"),
            log_level="INFO",
            verify_tls=True,
            request_timeout_seconds=1.0,
            terminal_idle_timeout_seconds=3600.0,
            terminal_reap_interval_seconds=30.0,
            max_terminal_sessions=8,
        )
        auth_state = AuthState(
            base_url=settings.base_url,
            cookies=[CookieState(name="jms_sessionid", value="cookie-value")],
        )
        manager = TerminalSessionManager()

        class HangingTerminal:
            def __init__(self, *args, **kwargs) -> None:
                self.cleanup_state = "not_attempted"
                self.cleanup_error = ""

            async def open(self) -> None:
                await asyncio.sleep(3600)

            async def close(self) -> None:
                self.cleanup_state = "closed"

        with patch("mcp_jumpserver_gui_sucks.terminal_manager.KoKoTerminalSession", HangingTerminal):
            with self.assertRaises(KoKoProbeError) as ctx:
                await manager.open_session(
                    settings,
                    auth_state,
                    asset_id="asset-uuid",
                    account="account-uuid",
                    startup_idle_timeout_seconds=0.1,
                )

        self.assertIn("Timed out opening the managed KoKo terminal session", str(ctx.exception))
        payload = await manager.list_sessions()
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["opening_count"], 0)


if __name__ == "__main__":
    unittest.main()
