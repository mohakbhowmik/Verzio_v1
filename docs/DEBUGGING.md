# Verzio Debugging Guide

Booking Flow

Customer
↓
WhatsApp
↓
Meta Webhook
↓
FastAPI
↓
Booking Runtime
↓
Booking Engine
↓
SQLite
↓
Admin Panel

When a booking fails:

1. Check webhook received.
2. Check business identified.
3. Check service exists.
4. Check appointment inserted.
5. Check confirmation sent.