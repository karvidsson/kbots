# Cloudflare

`cloudflare_zones`, `cloudflare_dns_list`, `cloudflare_dns_update`.

Install: `cp extras/cloudflare/cloudflare.py "$KBOTS_OVERLAY/tools/"`

Auth: vault key `secrets/cloudflare-api-token`. Consider HITL-gating
`cloudflare_dns_update` — DNS mistakes are outages.
