import unittest
from argparse import Namespace
from unittest.mock import AsyncMock, Mock, patch

from mcp_jumpserver_gui_sucks import cli, service


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

    async def test_execute_koko_command_payload_resolves_account_reference(self) -> None:
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
                service,
                "execute_koko_command",
                AsyncMock(return_value={"ws_connected": True, "command_sent": True}),
            ) as execute_mock,
        ):
            payload = await service.execute_koko_command_payload(
                asset_id="88fa41cf-c845-4efa-9b4b-534923b5a507",
                account="test-root",
                command="hostname",
            )

        execute_mock.assert_awaited_once()
        _, kwargs = execute_mock.await_args
        self.assertEqual(kwargs["asset_id"], "asset-uuid")
        self.assertEqual(kwargs["account"], "account-uuid")
        self.assertEqual(payload["resolved_target"], resolved_target)
        self.assertTrue(payload["terminal_auth"]["cookie_session_authenticated"])


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


if __name__ == "__main__":
    unittest.main()
