#!/usr/bin/env bash
# First-time setup
set -euo pipefail
cd "$(dirname "$0")"

echo "📦 Installing Python dependencies..."
pip3 install -r requirements.txt

echo "🌐 Installing Playwright Chromium..."
python3 -m playwright install chromium

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo ""
  echo "📝 Created .env — edit it now and add your login:"
  echo "   nano .env"
  echo ""
else
  echo "✓ .env already exists"
fi

chmod +x run.sh
echo ""
echo "✅ Setup complete! Run the bot with:"
echo "   ./run.sh"
