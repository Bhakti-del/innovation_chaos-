#!/bin/bash
set -e
cd "$(dirname "$0")"

echo '=== SmartCanteen AI Startup ==='

# Install dependencies if needed
if ! python3 -c 'import fastapi' 2>/dev/null; then
  echo 'Installing dependencies...'
  pip3 install -r requirements.txt
fi

# Generate synthetic data if not present
if [ ! -f data/sales_data.csv ]; then
  echo 'Generating synthetic sales data...'
  python3 data/generate.py
fi

# Train model if not present
if [ ! -f ml/model.pkl ]; then
  echo 'Training LightGBM model...'
  python3 ml/train.py
fi

echo 'Starting server on http://localhost:8000'
echo 'Student page: http://localhost:8000/static/student.html'
echo 'Staff page:   http://localhost:8000/static/staff.html'
echo 'Manager page: http://localhost:8000/static/manager.html'
echo ''
echo 'Demo credentials:'
echo '  Manager:  admin / admin123'
echo '  Staff:    staff1 / staff123'
echo '  Student:  student1 / student123'
echo ''
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
