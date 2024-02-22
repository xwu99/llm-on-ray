import unittest
from unittest.mock import patch
import logging
import os
from logger import get_logger


class TestLogger(unittest.TestCase):
    @patch.dict(os.environ, {"RANK": "1"})
    def test_get_logger_with_rank(self):
        logger = get_logger("test_logger_with_rank")
        self.assertEqual(logger.name, "test_logger_with_rank")
        self.assertEqual(logger.level, logging.INFO)
        self.assertIn("[Rank 1]", logger.handlers[0].formatter._fmt)

    @patch.dict(os.environ, {"RANK": "1"})
    def test_get_logger_without_rank(self):
        logger = get_logger("test_logger_without_rank", rank=None)
        self.assertEqual(logger.name, "test_logger_without_rank")
        self.assertEqual(logger.level, logging.INFO)
        self.assertIn("[Rank 1]", logger.handlers[0].formatter._fmt)

    def test_get_logger_with_rank_provided(self):
        logger = get_logger("test_logger_with_rank_provided", rank=2)
        self.assertEqual(logger.name, "test_logger_with_rank_provided")
        self.assertEqual(logger.level, logging.INFO)
        self.assertIn("[Rank 2]", logger.handlers[0].formatter._fmt)

    @patch.dict(os.environ, {}, clear=True)
    def test_get_logger_without_rank_and_env(self):
        logger = get_logger("test_logger_without_rank_and_env", rank=None)
        self.assertEqual(logger.name, "test_logger_without_rank_and_env")
        self.assertEqual(logger.level, logging.INFO)
        self.assertIn("[Rank -1]", logger.handlers[0].formatter._fmt)


if __name__ == "__main__":
    unittest.main()
