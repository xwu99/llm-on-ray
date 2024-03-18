#
# Copyright 2023 The LLM-on-Ray Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import sys
import asyncio
import functools
from ray import serve
from queue import Empty
import torch
from transformers import TextIteratorStreamer
from typing import List, Tuple, Union, Dict, Any
from starlette.requests import Request
from starlette.responses import StreamingResponse, JSONResponse
from fastapi import HTTPException
from llm_on_ray.inference.inference_config import InferenceConfig
from llm_on_ray.inference.api_openai_backend.openai_protocol import ModelResponse
from llm_on_ray.inference.utils import get_prompt_format, PromptFormat
from llm_on_ray.inference.logger import get_logger

logger = get_logger(__name__)


@serve.deployment
class PredictorDeployment:
    def __init__(self, infer_conf: InferenceConfig):
        self.device = torch.device(infer_conf.device)
        self.process_tool = None
        chat_processor_name = infer_conf.model_description.chat_processor
        prompt = infer_conf.model_description.prompt
        if chat_processor_name:
            try:
                module = __import__("chat_process")
            except Exception:
                sys.path.append(os.path.dirname(__file__))
                module = __import__("chat_process")
            chat_processor = getattr(module, chat_processor_name, None)
            if chat_processor is None:
                raise ValueError(
                    infer_conf.name
                    + " deployment failed. chat_processor("
                    + chat_processor_name
                    + ") does not exist."
                )
            self.process_tool = chat_processor(**prompt.dict())

        self.use_deepspeed = infer_conf.deepspeed
        self.use_vllm = infer_conf.vllm.enabled
        self.is_mllm = True if chat_processor_name in ["ChatModelwithImage"] else False

        if self.use_deepspeed:
            from llm_on_ray.inference.deepspeed_predictor import DeepSpeedPredictor

            self.predictor = DeepSpeedPredictor(infer_conf)
            self.streamer = self.predictor.get_streamer()
        elif self.use_vllm:
            from llm_on_ray.inference.vllm_predictor import VllmPredictor

            self.predictor = VllmPredictor(infer_conf)
        elif self.is_mllm:
            from llm_on_ray.inference.mllm_predictor import MllmPredictor

            self.predictor = MllmPredictor(infer_conf)
        else:
            from llm_on_ray.inference.transformer_predictor import TransformerPredictor

            self.predictor = TransformerPredictor(infer_conf)
        self.loop = asyncio.get_running_loop()

    def consume_streamer(self):
        for text in self.streamer:
            yield text

    async def consume_streamer_async(self, streamer: TextIteratorStreamer):
        while True:
            try:
                for token in streamer:
                    yield token
                break
            except Empty:
                # The streamer raises an Empty exception if the next token
                # hasn't been generated yet. `await` here to yield control
                # back to the event loop so other coroutines can run.
                await asyncio.sleep(0.001)

    # Handle streaming, only support single prompt
    async def handle_streaming(self, prompt: str, config: Dict[str, Any]) -> StreamingResponse:
        if isinstance(prompt, list):
            return JSONResponse(
                status_code=400,
                content="Streaming response is not supported when multiple prompts are provided.",
            )
        if self.use_deepspeed:
            self.predictor.streaming_generate(prompt, self.streamer, **config)
            return StreamingResponse(
                self.consume_streamer(), status_code=200, media_type="text/plain"
            )
        elif self.use_vllm:
            results_generator = await self.predictor.streaming_generate_async(prompt, **config)
            return StreamingResponse(
                self.predictor.stream_results(results_generator),
                status_code=200,
                media_type="text/plain",
            )
        else:
            streamer = self.predictor.get_streamer()
            self.loop.run_in_executor(
                None,
                functools.partial(self.predictor.streaming_generate, prompt, streamer, **config),
            )
            return StreamingResponse(
                self.consume_streamer_async(streamer), status_code=200, media_type="text/plain"
            )

    # Handle non-streaming, support single and multiple prompts
    async def handle_non_streaming(self, json_request: Dict[str, Any]) -> Union[JSONResponse, str]:
        # already checked text is not emptpy
        text = json_request["text"]
        config = json_request["config"] if "config" in json_request else {}
        if not isinstance(text, list) and not isinstance(text, str):
            return JSONResponse(
                status_code=400,
                content="Invalid prompt format from the request.",
            )

        # return prompt or list of prompts preprocessed
        prompts = self.preprocess_prompts(
            text, return_list=self.use_vllm or self.process_tool is not None
        )

        # Use vllm for continuous batching
        if self.use_vllm:
            return await self.predictor.generate_async(prompts, **config)
        else:
            # static batching
            if isinstance(prompts, list):
                return await self.handle_static_batch(prompts, **config)
            # dynamic batching
            return await self.handle_dynamic_batch(prompts)

        return JSONResponse(status_code=400, content="Error when handling non-streaming request.")

    # TODO: get max_batch_size from the serve config
    @serve.batch(max_batch_size=4)
    async def handle_dynamic_batch(self, json_requests: List[Dict[str, Any]]):
        logger.info(f"Handling dynamic batch (size={len(json_requests)}) ...")

        # batch prompts that configs are the same and also save request index
        batched_prompts: Dict[str, Tuple[List[str], List[int]]] = {}
        for i, json_request in enumerate(json_requests):
            prompt = json_request["text"]
            config = json_request["config"] if "config" in json_request else {}
            key = str(config)
            batched_prompts[key] = batched_prompts.get(str(config), ([], []))
            batched_prompts[key][0].append(prompt)
            batched_prompts[key][1].append(i)

        print(batched_prompts)

        # return results of each batch and fill in final results according to the request indices
        results = [None] * len(json_requests)
        for key, (prompts, indices) in batched_prompts.items():
            config = eval(key)
            batch_results = await self.predictor.generate_async(prompts, **config)
            for index, result in zip(indices, batch_results):
                results[index] = result
        return results

    async def handle_static_batch(self, prompts: List[str], **config: Dict[str, Any]):
        logger.info(f"Handling static batch (size={len(prompts)}) ...")
        # Still use dynamic batching for vllm
        if self.use_vllm:
            return await self.predictor.generate_async(prompts, **config)
        else:
            return self.predictor.generate(prompts, **config)

    def preprocess_prompts(
        self, text: Union[str, List[str]], return_list=True
    ) -> Union[str, List[str], None]:
        """
        Preprocesses the prompts for non-streaming inference.

        Args:
            text (Union[str, List[str]]): The input text or list of texts to be preprocessed.
            return_list (bool, optional): Whether to return the preprocessed prompts as a list. Defaults to True.

        Returns:
            Union[str, List[str], None]: The preprocessed prompts.
            If the input `text` is a list, the preprocessed prompt is always returned as a list except it's processed by
                process tool and return_list is False.
            If the input `text` is a str,
                If `return_list` is True, a list of preprocessed prompts is returned.
                If `return_list` is False, a single preprocessed prompt is returned as a string.

        """
        if isinstance(text, list):
            prompts = []
            prompt_format = get_prompt_format(text)
            if prompt_format == PromptFormat.CHAT_FORMAT:
                if self.process_tool is not None:
                    prompt = self.process_tool.get_prompt(text)
                    return [prompt] if return_list else prompt
                else:
                    prompts.extend(text)
                    return prompts
            elif prompt_format == PromptFormat.PROMPTS_FORMAT:
                prompts.extend(text)
                return prompts
        else:
            return [text] if return_list else text

        return None

    async def __call__(self, http_request: Request) -> Union[StreamingResponse, JSONResponse, str]:
        try:
            json_request: Dict[str, Any] = await http_request.json()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content="Invalid JSON format from http request.",
            )

        streaming_response = json_request["stream"] if "stream" in json_request else False
        text = json_request["text"] if "text" in json_request else ""
        if text == "":
            return JSONResponse(
                status_code=400,
                content="Empty prompt is not supported.",
            )
        config = json_request["config"] if "config" in json_request else {}

        # Handle streaming response
        if streaming_response:
            return await self.handle_streaming(text, config)

        return await self.handle_non_streaming(json_request)

    async def openai_call(self, prompt, config, streaming_response=True):
        prompts = []
        images = []
        if isinstance(prompt, list):
            prompt_format = get_prompt_format(prompt)
            if prompt_format == PromptFormat.CHAT_FORMAT:
                if self.process_tool is not None:
                    if self.is_mllm:
                        prompt, image = self.process_tool.get_prompt(prompt)
                        prompts.append(prompt)
                        images.extend(image)
                    else:
                        prompt = self.process_tool.get_prompt(prompt)
                        prompts.append(prompt)
                else:
                    prompts.extend(prompt)
            elif prompt_format == PromptFormat.PROMPTS_FORMAT:
                yield HTTPException(
                    400, "Mulitple prompts are not supported when using openai compatible api."
                )
            else:
                yield HTTPException(400, "Invalid prompt format.")
        else:
            prompts.append(prompt)

        if not streaming_response:
            if self.use_vllm:
                generate_result = (await self.predictor.generate_async(prompts, **config))[0]
                generate_text = generate_result.text
            elif self.is_mllm:
                generate_result = self.predictor.generate(images, prompts, **config)
                generate_text = generate_result.text[0]
            else:
                generate_result = self.predictor.generate(prompts, **config)
                generate_text = generate_result.text[0]
            model_response = ModelResponse(
                generated_text=generate_text,
                num_input_tokens=generate_result.input_length,
                num_input_tokens_batch=generate_result.input_length,
                num_generated_tokens=generate_result.generate_length,
                preprocessing_time=0,
            )
            yield model_response
        else:
            if self.use_deepspeed:
                self.predictor.streaming_generate(prompts, self.streamer, **config)
                response_handle = self.consume_streamer_async(self.streamer)
            elif self.use_vllm:
                # TODO: streaming only support single prompt
                # It's a wordaround for current situation, need another PR to address this
                if isinstance(prompts, list):
                    prompt = prompts[0]
                results_generator = await self.predictor.streaming_generate_async(prompt, **config)
                response_handle = self.predictor.stream_results(results_generator)
            elif self.is_mllm:
                streamer = self.predictor.get_streamer()
                self.loop.run_in_executor(
                    None,
                    functools.partial(
                        self.predictor.streaming_generate, images, prompts, streamer, **config
                    ),
                )
                response_handle = self.consume_streamer_async(streamer)
            else:
                streamer = self.predictor.get_streamer()
                self.loop.run_in_executor(
                    None,
                    functools.partial(
                        self.predictor.streaming_generate, prompts, streamer, **config
                    ),
                )
                response_handle = self.consume_streamer_async(streamer)
            input_length = self.predictor.input_length
            async for output in response_handle:
                if not input_length:
                    input_length = self.predictor.input_length
                model_response = ModelResponse(
                    generated_text=output,
                    num_input_tokens=input_length,
                    num_input_tokens_batch=input_length,
                    num_generated_tokens=1,
                    preprocessing_time=0,
                )
                yield model_response
