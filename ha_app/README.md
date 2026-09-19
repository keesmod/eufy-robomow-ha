# Eufy Mower Bridge

Dedicated bridge for the Eufy E15 mower, consumed by the
[Eufy Robomow integration](https://github.com/keesmod/eufy-robomow-ha) through
its `bridge` backend. This is a local install candidate. It is not listed in an
app repository and no prebuilt image is published.

The bridge signs in to your Eufy account with its own credentials, discovers the
mower and reads its state over the LAN. It has no connection with the camera
bridge of the Eufy Security Viewer and must use its own token, port and data.

## Install

1. Copy the `ha_app` folder of the repository to `/addons/eufy_mower_bridge` on
   your Home Assistant OS machine, for example through the Samba or SSH app.
2. Open **Settings → Apps → Install app**, refresh, and install **Eufy Mower
   Bridge** from the local apps section. The first install builds the container
   from source and can take several minutes.
3. In **Configuration** set `token` to a unique random secret of at least 32
   characters, `email`, `password` and the two-letter `country` of your Eufy
   account. Set `host` to the mower's LAN address when you have one mower, or
   `hosts` to `id=host` pairs after the first discovery. Save.
4. Start the app and check **Logs**. The bridge reports one authentication
   attempt and the number of discovered mowers.
5. In the integration, choose the `bridge` backend with the URL
   `http://<home-assistant-ip>:8090` and the same token.

## Operate

The app stores the bridge identity and the Eufy session in `/data/eufy-mower`.
Back up the app and keep the same token when updating. Restarting the app makes
one new authentication attempt. Details, upgrade and rollback steps are in the
[deployment guide](https://github.com/keesmod/eufy-robomow-ha/blob/main/docs/bridge-deployment.md).

This version serves state only. It issues no mower command and stays in
`observe_only`.
