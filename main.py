import asyncio
import logging
import numpy as np
import time
import json
import os
import tempfile
import requests

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from aiohttp import ClientSession
from langchain.text_splitter import RecursiveCharacterTextSplitter
from datasets import Dataset, load_dataset
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio

from models import chunk_config, embed_config, WebhookPayload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# you token from Settings
HF_TOKEN = os.getenv("HF_TOKEN")

# URL of TEI endpoint
TEI_URL = os.getenv("TEI_URL")
# name of chunked dataset
CHUNKED_DS_NAME = os.getenv("CHUNKED_DS_NAME")
# name of embeddings dataset
EMBED_DS_NAME = os.getenv("EMBED_DS_NAME")
# splits of input dataset to process, comma separated
INPUT_SPLITS = os.getenv("INPUT_SPLITS")
# name of column to load from input dataset
INPUT_TEXT_COL = os.getenv("INPUT_TEXT_COL")

INPUT_SPLITS = [spl.strip() for spl in INPUT_SPLITS.split(",") if spl]

app = FastAPI()
app.state.seen_Sha = set()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/webhook")
async def post_webhook(
        payload: WebhookPayload,
        task_queue: BackgroundTasks
):
    if not (
        payload.event.action == "update"
        and payload.event.scope.startswith("repo.content")
        and payload.repo.type == "dataset"
        # webhook posts multiple requests with the same update, this addresses that
        and payload.repo.headSha not in app.state.seen_Sha
    ):
        # no-op
        logger.info("Update detected, no action taken")
        return {"processed": False}

    app.state.seen_Sha.add(payload.repo.headSha)
    task_queue.add_task(chunk_dataset, ds_name=payload.repo.name)
    task_queue.add_task(embed_dataset, ds_name=CHUNKED_DS_NAME)

    return {"processed": True}


"""
CHUNKING
"""

class Chunker:
    def __init__(self, strategy, split_seq=".", chunk_len=512):
        self.split_seq = split_seq
        self.chunk_len = chunk_len
        if strategy == "recursive":
            self.split = RecursiveCharacterTextSplitter(
                chunk_size=chunk_len,
                separators=[split_seq]
            ).split_text
        if strategy == "sequence":
            self.split = self.seq_splitter
        if strategy == "constant":
            self.split = self.const_splitter

    def seq_splitter(self, text):
        return text.split(self.split_seq)

    def const_splitter(self, text):
        return [
            text[i * self.chunk_len:(i + 1) * self.chunk_len]
            for i in range(int(np.ceil(len(text) / self.chunk_len)))
        ]


def chunk_generator(input_dataset, chunker):
    for i in tqdm(range(len(input_dataset))):
        chunks = chunker.split(input_dataset[i][INPUT_TEXT_COL])
        for chunk in chunks:
            if chunk:
                yield {INPUT_TEXT_COL: chunk}


def chunk_dataset(ds_name):
    logger.info("Update detected, chunking is scheduled")
    input_ds = load_dataset(ds_name, split="+".join(INPUT_SPLITS))
    chunker = Chunker(
        strategy=chunk_config.strategy,
        split_seq=chunk_config.split_seq,
        chunk_len=chunk_config.chunk_len
    )

    dataset = Dataset.from_generator(
        chunk_generator,
        gen_kwargs={
            "input_dataset": input_ds,
            "chunker": chunker
        }
    )

    dataset.push_to_hub(
        CHUNKED_DS_NAME,
        private=chunk_config.private,
        token=HF_TOKEN
    )

    logger.info("Done chunking")
    return {"processed": True}


"""
EMBEDDING
"""

async def embed_sent(sentence, semaphore, tmp_file):
    async with semaphore:
        payload = {
            "inputs": sentence,
            "truncate": True
        }

        async with ClientSession(
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {HF_TOKEN}"
            }
        ) as session:
            async with session.post(TEI_URL, json=payload) as resp:
                if resp.status != 200:
                    raise RuntimeError(await resp.text())
                result = await resp.json()

                tmp_file.write(
                    json.dumps({"vector": result[0], INPUT_TEXT_COL: sentence}) + "\n"
                )


async def embed(input_ds, temp_file):
    semaphore = asyncio.BoundedSemaphore(embed_config.semaphore_bound)
    jobs = [
        asyncio.create_task(embed_sent(row[INPUT_TEXT_COL], semaphore, temp_file))
        for row in input_ds if row[INPUT_TEXT_COL].strip()
    ]
    logger.info(f"num chunks to embed: {len(jobs)}")

    tic = time.time()
    await tqdm_asyncio.gather(*jobs)
    logger.info(f"embed time: {time.time() - tic}")


def wake_up_endpoint(url):
    logger.info("Starting up TEI endpoint")
    n_loop = 0
    while requests.get(
        url=url,
        headers={"Authorization": f"Bearer {HF_TOKEN}"}
    ).status_code != 200:
        time.sleep(2)
        n_loop += 1
        if n_loop > 40:
            raise TimeoutError("TEI endpoint is unavailable")
    logger.info("TEI endpoint is up")


def embed_dataset(ds_name):
    logger.info("Update detected, embedding is scheduled")
    wake_up_endpoint(TEI_URL)
    input_ds = load_dataset(ds_name, split="train")
    with tempfile.NamedTemporaryFile(mode="a", suffix=".jsonl") as temp_file:
        asyncio.run(embed(input_ds, temp_file))

        dataset = Dataset.from_json(temp_file.name)
        dataset.push_to_hub(
            EMBED_DS_NAME,
            private=embed_config.private,
            token=HF_TOKEN
        )

    logger.info("Done embedding")
    return {"processed": True}


# For debugging

# import uvicorn
# if __name__ == "__main__":
#     uvicorn.run(app, host="0.0.0.0", port=7860)
