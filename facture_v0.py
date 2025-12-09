import requests
import json
import base64
from pathlib import Path
import pandas as pd
import pprint

url = "https://openrouter.ai/api/v1/chat/completions"
headers = {
    "Authorization": f"Bearer {"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"}",
    "Content-Type": "application/json"
}


def encode_image_to_base64(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# Read and encode the image
image_path = "image1.png"
base64_image = encode_image_to_base64(image_path)
data_url = f"data:image/jpeg;base64,{base64_image}"

messages = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "List all the items on this receipt with their prices. Respond in JSON format."
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": data_url
                }
            }
        ]
    }
]

payload = {
    "model": "google/gemini-2.0-flash-001",
    "messages": messages
}

response = requests.post(url, headers=headers, json=payload)

json_resp = response.json()

#pprint.pprint(json_resp)


json_str = json_resp.get("choices", [])[0].get("message", {}).get("content", "No content found")
json_object = json.loads(json_str.replace('json', '').replace('```', ''))


df = pd.DataFrame(json_object)

df.to_excel("extracted.xlsx", index=False)


print(df)
