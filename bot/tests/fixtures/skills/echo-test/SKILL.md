---
name: echo-test
description: A test skill that echoes arguments back
tags: [test, echo]
tools:
  - name: echo_test
    description: Echo arguments back as JSON. Use for testing the skill execution engine.
    availability: search
    script: scripts/echo.py
    parameters:
      message:
        type: string
        description: Message to echo back
    timeout: 10
---

# Echo Test Skill

A simple test skill used to verify the skill execution engine works correctly.
