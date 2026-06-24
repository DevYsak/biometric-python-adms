@echo off
echo Installing pyzk...
py -m pip install pyzk

echo Running biometric test...
py test_biometric.py

pause
