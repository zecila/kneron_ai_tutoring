from fastmcp import FastMCP
import os
from pathlib import Path
import subprocess
import uvicorn
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
from urllib.parse import unquote
import sys
import re

mcp = FastMCP('manim-mcp-server')
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

@mcp.tool
def get_context(docs_list_str: str) -> str:
    """Retrieve context for a given query.

    Args:
        docs_list_str (str): A list of docs to retrieve, separated by ",".

    Returns:
        str: A string of all concatenated documents.
    """
    docs_list = docs_list_str.strip("\n").split(",")
    ret = ""
    if os.getcwd().endswith("manimations"):
        os.chdir("..")
    for doc in docs_list:
        doc = doc.strip().strip(":").strip()
        with open(f"custom_docs/{doc}.txt", "r") as f:
            ret += f.read() + "\n\n"
    return ret
        
pairs = [('\a', '\\a'), (r"\x07", '\\a'), ('\b', '\\b'), ('\f', '\\f'), ('\r', '\\r'), ('\t', '\\t'), ('\v', '\\v')]        

@mcp.tool
def generate_video(code: str, job_id: str) -> str:
    """Generates video based on urllib.parse.quote'd code, isolated per job_id
    (e.g. "{lesson_id}_{slide_id}") so concurrent renders don't collide.
    """
    if not _JOB_ID_RE.match(job_id):
        raise RuntimeError(f"Invalid job_id: {job_id!r}")

    if os.getcwd().endswith("manimations"):
        os.chdir("..")
    code = unquote(code)
    code = code.replace("```python", "").replace("```", "")
    for og, rep in pairs:
        code = code.replace(og, rep)

    target_dir = Path("manimations") / job_id
    target_dir.mkdir(parents=True, exist_ok=True)
    orig_dir = os.getcwd()
    os.chdir(target_dir)

    with open("main.py", "w") as f:
        f.write(code)

    manim_cmd = [sys.executable, "-m", "manim", "-qm", "main.py", "Main"]
    result = subprocess.run(manim_cmd, capture_output=True, text=True)

    os.chdir(orig_dir)
    if result.returncode != 0:
        raise RuntimeError(f"Manim command failed with error: {result.stderr}")

    return ""
    
app = mcp.http_app()

async def download_file(request):
    if os.getcwd().endswith("manimations"):
        os.chdir("..")
    job_id   = request.path_params['job_id']
    filename = request.path_params['filename']
    if not _JOB_ID_RE.match(job_id):
        return JSONResponse({"error": "Invalid job_id"}, status_code=400)
    file_path = f'manimations/{job_id}/media/videos/main/720p30/{filename}'
    
    if not os.path.exists(file_path):
        return JSONResponse({"error": "File not found"}, status_code=404)
    
    mtime = os.path.getmtime(file_path)
    
    return FileResponse(
        file_path,
        media_type='video/mp4',
        filename=filename,
        headers={
            "Cache-Control": "no-cache",  # Prevent caching of potentially changing files
            "ETag": f'"{mtime}"'  # Use modification time as ETag
        }
    )
    
download_route = Route('/download/{job_id}/{filename}', download_file, methods=['GET'])
app.routes.append(download_route)

def main():
    uvicorn.run(app, host='0.0.0.0', port=8000)
    
if __name__ == "__main__":
    main()