# Portfolio cards

Generated from commercial_portfolio_spec_exporter.py; source terms are in portfolio.json.

- v2ex.md: rich Markdown with package payment links.
- eleduck.md: remote developer profile and scoped offers.
- github.md: pasteable GitHub Profile README hero and package section.
- xianyu.txt: plain text with platform-native ordering and support guidance.
- terminal.ansi: ANSI showcase; run `cat terminal.ansi`.
- Each platform directory contains PKG-01, PKG-02 and PKG-03 separately.

Local Markdown links assume the bundle layout. When pasting into a website, host portfolio.json
and payments.md and update those links. HTTPS payment links supplied via --payment-links
are already absolute. Unconfigured payment links open instructions, not a checkout.

Regenerate into a new directory with:

    python3 scripts/cross_platform_portfolio_card_renderer.py --output /tmp/portfolio-cards

Payment JSON example (replace with your confirmed HTTPS destinations):

    {"PKG-01": "https://your-payment-host/confirmed-order"}

The renderer does not publish cards, process payments, or execute acceptance tests.
Markdown pixels depend on the host font; terminal geometry uses Unicode display cells.
manifest.json hashes every other artifact as UTF-8 bytes with LF endings.
