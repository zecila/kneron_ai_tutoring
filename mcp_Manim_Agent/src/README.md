## Manim Agent Tutorial

### Introduction
This guide provides step-by-step instructions for how to run a Manim Agent in Dify, using either the `llm_en_v_1_3_1` or `llm_zh_1_3_1` model.

### Prerequisites
Before starting, ensure you have the following:
1. Dify (at least v1.6.0) installed and running via Docker. Do not run on `localhost`
2. Access to a LLM endpoint
3. Model `llm_en_v_1_3_1` or `llm_zh_1_3_1` accessible from your endpoint

### Step 1: Add LLM Model to Dify
1. Click the user icon located in the top-right corner of the interface
2. Select “Settings” from the dropdown menu 
3. Navigate to the “Model Provider” tab
4. Install the “OpenAI-API-compatible" model if not already installed
5. In the “OpenAI-API-compatible" model, click “Add Model” and configure as follows:
    a. Model Type: LLM
    b. Model Name: `llm_en_v_1_3_1` or `llm_zh_v_1_3_1`
    c. API Key: leave blank
    d. API endpoint URL: `http://<your-server-ip>:5001/openai/v1`
    e. Model context size: 4096

### Step 2: Set up the MCP Server
1. Start the MCP server by navigating to this project directory in the server and running `python server.py`
2. At the top of the Dify interface, navigate to the “Tool” tab
3. Click the "MCP" tab
4. Click "Add MCP Server (HTTP)" and configure as follows:
    a. Server URL: `http://host.docker.internal:8000/mcp/`
    b. Name & Icon: Manim MCP Server
    c. Server Identifier: manim-mcp-server

### Step 3: Create the MCP Agent in Dify
1. On the main interface in Dify, click "Import DSL file" in the "Create App" block
2. Select `Manim Agent.yml` in this directory