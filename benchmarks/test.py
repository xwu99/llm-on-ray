from omegaconf import OmegaConf

conf = OmegaConf.create(
    {
        "model_endpoint_base": "http://127.0.0.1:8000",
        "model_name": "gpt-j-6b",
        "dataset": "./ShareGPT_V3_unfiltered_cleaned_split.json",
        "num_prompts": 5,
        "request_rate": float("inf"),
        "seed": 0,
        "trust_remote_code": False,
        "max_new_tokens": "10",
        "temperature": None,
        "top_p": None,
        "top_k": None,
    }
)
# print(OmegaConf.to_yaml(conf))
# OmegaConf.save(conf, "config.yaml")

args = OmegaConf.load("config.yaml")
print(args.model_endpoint_base)
