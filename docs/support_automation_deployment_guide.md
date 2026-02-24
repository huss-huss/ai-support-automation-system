# 🚀 Deployment Guide

# Multi‑Agent Customer Support Automation

## VPS Setup

sudo apt update && sudo apt upgrade -y sudo apt install docker.io
docker-compose nginx git -y

## Repo Structure

support-automation-system/ backend/ worker/ frontend/ infra/ docs/

## Start Services

docker-compose up -d --build

## Environment Variables

OPENAI_API_KEY=your_key POSTGRES_DB=support REDIS_URL=redis://redis:6379

## CI/CD Flow

Push → GitHub Actions → Build Docker → SSH Deploy → Restart Services
