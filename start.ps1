<#
.SYNOPSIS
    Agent-OS Start Script
.DESCRIPTION
    Starts the Agent-OS server using the virtual environment.
#>

if ($PSScriptRoot) {
    Set-Location $PSScriptRoot
}

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "           Starting Agent-OS             " -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

if (!(Test-Path -Path "venv\Scripts\python.exe")) {
    Write-Host "Error: Virtual environment not found. Please run .\install.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "Starting server..." -ForegroundColor Yellow
.\venv\Scripts\python.exe main.py
