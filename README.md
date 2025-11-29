SAM-Radar (Render-ready)

Files:
- app.py          : Streamlit application
- requirements.txt: Python dependencies
- render.yaml     : Render web service configuration

Deployment:
1. Push these files to a GitHub repository.
2. On Render, create a new Web Service and connect the repo.
3. Render will run the build and start commands from render.yaml.
4. Use Live Tail logs to inspect fetch results (GNews / NewsLookup counts).

Notes:
- GNews uses a demo token for small-scale testing. For production, obtain a paid API key.
- If both sources return zero, your hosting may block outbound requests; check logs.
