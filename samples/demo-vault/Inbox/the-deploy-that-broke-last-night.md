---
id: 01M1M2AF0TZ64ZTM21N2YXGWW6
title: the deploy that broke last night
kind: image
status: open
topics: []
entities: []
tags: []
media: .mnemo/media/f29010636b5c39c6.png
created: 2026-09-03T16:41:05Z
updated: 2026-10-21T17:22:38Z
---

the deploy that broke last night

Deployment Failure Notification - PostgreSQL, Production Server, "10.0.4.17"


Software: psycopg2, PostgreSQL
Environment: Production
Server Address: 10.0.4.17
Port: 5432
Person: Vinayak (Triggered by)
Build Number: #4821
Rollback Target: #4820


Text in image

Deployment failed
Build #4821 on production

ERROR: connection refused on port 5432
psycopg2.OperationalError: could not connect
to server at 10.0.4.17

Retry deployment
Rollback to #4820

Duration: 3m 42s
Triggered by: vinayak
