# FILEPATH: /home/xiaochang/Works/llm-on-ray-xwu99/tests/inference/test_vllm_predictor.py

import asynctest
from unittest.mock import patch, MagicMock
from inference.vllm_predictor import VllmPredictor
from inference.inference_config import InferenceConfig


class TestVllmPredictor(asynctest.TestCase):
    @patch("inference.vllm_predictor.AsyncLLMEngine")
    def setUp(self, mock_engine):
        infer_conf = InferenceConfig()
        self.predictor = VllmPredictor(infer_conf)
        self.mock_engine = mock_engine

    async def test_generate_async_single_prompt(self):
        self.mock_engine.generate.return_value = self._mock_generator(["Hello, world!"])
        result = await self.predictor.generate_async("Hello")
        self.assertEqual(result, "Hello, world!")

    async def test_generate_async_multiple_prompts(self):
        self.mock_engine.generate.return_value = self._mock_generator(["Hello, world!"])
        results = await self.predictor.generate_async(["Hello", "Hi"])
        self.assertEqual(results, ["Hello, world!", "Hello, world!"])

    async def test_streaming_generate_async(self):
        self.mock_engine.generate.return_value = self._mock_generator(["Hello, world!"])
        result_generator = await self.predictor.streaming_generate_async("Hello")
        results = [result async for result in result_generator]
        self.assertEqual(results, ["Hello, world!"])

    async def test_stream_results(self):
        results_generator = self._mock_generator(["Hello, world!"])
        result_generator = self.predictor.stream_results(results_generator)
        results = [result async for result in result_generator]
        self.assertEqual(results, ["Hello, world!"])

    def _mock_generator(self, values):
        for value in values:
            yield MagicMock(outputs=[MagicMock(text=value)], finished=True)


if __name__ == "__main__":
    asynctest.main()
