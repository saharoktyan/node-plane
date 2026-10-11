[Русская версия](troubleshooting.ru.md)

# Troubleshooting

Start with the operation's error and diagnostics. Record the installed version, affected node and operation ID. Remove tokens, private keys, DSNs and configuration contents before sharing output.

## Controller or bot is unavailable

On the controller:

```sh
cd /opt/node-plane/current
./scripts/healthcheck.sh --mode simple
sudo journalctl -u node-plane-backend -u node-plane-telegram -u node-plane-driver --since '10 minutes ago' -n 150 --no-pager
sudo journalctl -u node-plane-backend-worker --since '10 minutes ago' -n 150 --no-pager
```

Check database availability and whether the worker service is active. Send `/start` again if a chat was cleared. Only one bot process should poll the same token.

## Workstation cannot connect

Check the selected host, SSH port, login and network route. Verify the host fingerprint before accepting a new key. A changed key can be a reinstalled host or a different host; confirm it independently.

For `permission_denied` / HTTP 403, check the administrator role, account availability and Workstation key binding. Root SSH access and backend account permission are separate checks. Inspect Workstation audit and controller logs; do not repeatedly enroll the same key under random accounts.

If installation state is unconfirmed, use **Check installation**. Check local Workstation updates separately from controller updates.

## Agent unavailable or setup blocked

On the affected node:

```sh
sudo systemctl status node-plane-agent --no-pager
sudo journalctl -u node-plane-agent --since '10 minutes ago' -n 150 --no-pager
```

Check the agent address, controller reachability and mutual-TLS configuration. A service marked running may still be starting. Inspect the current setup phase and use the offered recovery action after resolving the connection problem.

## Runtime installed but not connecting

Check node health, public DNS/IP and the intended TCP/UDP ports. Confirm that the client has a current configuration and supports its protocol. For AWG, use different device configurations for simultaneous clients.

```sh
sudo docker ps -a
```

Do not publish full container configuration or VPN credentials in a bug report.

## Unknown operation outcome

Blocked means the system needs observation or recovery before it can safely continue. Inspect the original operation rather than issuing a second reinstall/delete. Keep driver and node journals. For removal, check the reported leftover resource and independent host verification result.

## Interface problems

Use at least 70 columns × 30 rows in Workstation. Scroll long pages with the wheel or scrollbar. If a clipboard action is unavailable, save the configuration file. Use Telegram's separate-message action for copying on iOS.

A useful bug report includes the release, OS/terminal or Telegram client, exact screen path, reproduction steps and secret-free logs. Distinguish a confirmed failed action from an outcome that has not yet been observed.
