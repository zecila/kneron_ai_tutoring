import requests
import json
from urllib.parse import quote, unquote
import os

lb_addr = ''
user_token_g = ["9424", "123", "789"]
client_id = "123"

def login(host_ip: str, email: str, password: str) -> str:
    global lb_addr, user_token_g
    if lb_addr != '':
        return ''
    lb_addr = f"https://{host_ip}:5002/load_balancer/"
    try:
        data = {"email": email, "password": password}
        response = requests.post(f'{lb_addr}lb_login', json=data, verify=False, timeout=60)
        if response.status_code != 200:
            return response.status_code + ": " + str(response.json())
        else:
            res_data = response.json()
            print(res_data)
            if 'user_token' in res_data:
                user_token_g = res_data["user_token"]
            return "200: " + res_data['status']
    except requests.exceptions.Timeout:
        return "504: Request timed out. Please check whether the target machine is reachable."
    except requests.RequestException as e:
        return "401: lb_login failed"
    
def upload_file(file_name: str):
    try:
        unique_vs_id = "public/manim_mcp"
        language = "English"
        file_base_path = "manim_docs/"
        test_files = [f"manim_docs/{file_name}"]
        file_relpaths = [file_name]

        data = {"unique_vs_id": unique_vs_id,
                "language": language,
                "file_base_path": file_base_path,
                "user_token": json.dumps(user_token_g),
                "client_id": '123',
                }


        files = []
        to_close = []
        for i in range(len(test_files)):
            test_file = test_files[i]
            file_rel_path = file_relpaths[i]
            print(test_file)
            print(file_rel_path)
            fp = open(test_file, 'rb')
            files.append(('file', (file_rel_path, fp, 'application/octet-stream')))
            to_close.append(fp)
        # Timeout should be set according to the deployment requirement for this flask API calling, such as timeout=21600, which is 6 hours.
        # Depend on the maximum file size and number of files supported for one upload.
        print(files)
        response = requests.post(lb_addr+"lb_upload_folder_to_vector_store", files=files, data=data, verify=False, timeout=21600)
        for a in to_close:
            print(f'closing file {a}')
            a.close()
        return str(response.json())
    except requests.RequestException as e:
        for a in to_close:
            print(f'closing file {a}')
            a.close()
        return ">>> lb_upload_folder_to_vector_store failed"
    
def remove_file(file_name: str):
    files = ["manim_docs/" + file_name]
    unique_vs_id = "public/manim_mcp"
    language = "English"
    try:
        data = {"files": files,
                "language": language,
                "unique_vs_id": unique_vs_id,
                "user_token": user_token_g,
                "client_id": '123'
                }

        response = requests.post(lb_addr + "lb_rm_from_vector_store", json=data, verify=False, timeout=60)
        return str(response.json())
    except requests.RequestException as e:
        return f">>> lb_rm_from_vector_store failed"
    
def list_files() -> str:
    try:
        unique_vs_id = "public/manim_mcp"
        language = "English"
        data = {"unique_vs_id": unique_vs_id,
                "language": language,
                "user_token": user_token_g}

        response = requests.post(lb_addr + "lb_get_file_list", json=data, verify=False, timeout=60)
        return ", ".join(response.json()["file_list"])
    except requests.RequestException as e:
        return ">>> lb_get_file_list failed"
    
def retrieve_context(query: str):
    try:
        """Test standard English query."""
        unique_vs_id = "public/manim_mcp"
        language = "English"
        client_id = "123"
        data = {"query": quote(query),
                "unique_vs_id": unique_vs_id,
                "language": language,
                "user_token": user_token_g,
                "client_id": client_id,
                "top_k": 3,
                }

        response = requests.post(lb_addr+"lb_get_related_docs", json=data, stream=True, verify=False, timeout=600)
        if response.status_code != 200:
            print(response.status_code)
        else:
             res_data = response.json()
             print(res_data)
        return ", ".join([related_doc['page_content'] for related_doc in res_data['related_docs']])

    except requests.RequestException as e:
        print(e)
        return ">>> lb_get_answer failed"
    
def main():
    login("192.168.200.134", "admin@useradmin.com", "admin123")
    print(retrieve_context('circle'))
    # docs_dir = "manim_docs"
    # if not os.path.exists(docs_dir):
    #     print(f"Directory '{docs_dir}' does not exist.")
    #     return
    # files = [f for f in os.listdir(docs_dir) if f.endswith('.txt')]
    # print(f"Found {len(files)} documentation files to upload.")
    # uploaded = 0
    # for fname in files:
    #     print(f"Uploading: {fname}")
    #     result = upload_file(fname)
    #     print(f"Result: {result}")
    #     uploaded += 1
    #     print(f"Uploaded {uploaded} documentation files.")
    # print(f"Finished uploading {uploaded} files.")
    
if __name__ == "__main__":
    main()
