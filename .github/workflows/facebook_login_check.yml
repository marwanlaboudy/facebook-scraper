name: Facebook Marketplace Scraper
on:
  workflow_dispatch:
  repository_dispatch:
    types: [run-marketplace-scraper]

jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Load Facebook session
        env:
          FB_SESSION: ${{ secrets.FB_SESSION }}
        run: |
          printf '%s' "$FB_SESSION" > facebook_session.json
          echo "File size:"
          wc -c facebook_session.json
          python3 -c "import json; json.load(open('facebook_session.json')); print('✅ JSON valid')" || echo "❌ JSON invalid"

      - name: Install dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y python3 python3-pip xvfb fonts-freefont-ttf
          pip install playwright gspread oauth2client
          playwright install --with-deps chromium

      - name: Run scraper
        env:
          GOOGLE_CREDS: ${{ secrets.GOOGLE_CREDS }}
        run: |
          xvfb-run -a python3 scraper.py

      - name: Save results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: results
          path: |
            marketplace_results.json
            marketplace_hrefs.json
            marketplace_loaded.png
            not_logged_in.png
            *.png
