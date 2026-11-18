"""Provider-independent structured model gateway with deterministic offline fixtures."""
from __future__ import annotations
import asyncio, json, os, time
from dataclasses import dataclass
from typing import Any, Protocol, Type
import httpx
from pydantic import BaseModel, ValidationError

AI_DISCLOSURE = (
    "You are an AI assistant. Never claim or imply that you are human. "
    "When drafting outbound communication, clearly disclose that the sender is an AI assistant "
    "acting on behalf of the named organisation; do not fabricate human identity or consent."
)

@dataclass
class ModelResponse:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str = "mock"
    model: str = "fixture"
    estimated_cost_inr: float = 0.0

class ModelProvider(Protocol):
    async def generate(self, *, model: str, messages: list[dict[str,str]], temperature: float=0) -> ModelResponse: ...

class MockModelProvider:
    """Fixture-driven provider; responses are consumed in order and are deterministic."""
    def __init__(self, fixtures: list[Any] | dict[str,Any] | None=None):
        self.fixtures=list(fixtures.values()) if isinstance(fixtures,dict) else list(fixtures or [])
        self.calls=[]
    async def generate(self, *, model, messages, temperature=0):
        self.calls.append({"model":model,"messages":messages})
        if not self.fixtures: raise RuntimeError("MockModelProvider has no fixture response")
        value=self.fixtures.pop(0)
        if isinstance(value,Exception): raise value
        if isinstance(value,dict): value=json.dumps(value)
        return ModelResponse(str(value),len(json.dumps(messages))//4, len(str(value))//4,"mock",model,0.0)

class OpenAICompatibleProvider:
    def __init__(self, base_url: str | None=None, api_key: str | None=None, timeout: float=30):
        self.base_url=(base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key=api_key or os.getenv("OPENAI_API_KEY")
        self.timeout=timeout
    async def generate(self, *, model, messages, temperature=0):
        headers={"Authorization":f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response=await client.post(self.base_url+"/chat/completions",headers=headers,json={"model":model,"messages":messages,"temperature":temperature})
            response.raise_for_status(); data=response.json()
        usage=data.get("usage",{})
        return ModelResponse(data["choices"][0]["message"]["content"],usage.get("prompt_tokens",0),usage.get("completion_tokens",0),"openai-compatible",model)

class AnthropicProvider:
    def __init__(self, api_key: str | None=None, base_url: str="https://api.anthropic.com", timeout: float=30):
        self.api_key=api_key or os.getenv("ANTHROPIC_API_KEY"); self.base_url=base_url.rstrip("/"); self.timeout=timeout
    async def generate(self, *, model, messages, temperature=0):
        system="\n".join(m["content"] for m in messages if m["role"]=="system")
        conversation=[m for m in messages if m["role"]!="system"]
        headers={"x-api-key":self.api_key or "", "anthropic-version":"2023-06-01","content-type":"application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response=await client.post(self.base_url+"/v1/messages",headers=headers,json={"model":model,"system":system,"messages":conversation,"max_tokens":4096,"temperature":temperature})
            response.raise_for_status(); data=response.json()
        usage=data.get("usage",{})
        return ModelResponse("".join(x.get("text","") for x in data.get("content",[])),usage.get("input_tokens",0),usage.get("output_tokens",0),"anthropic",model)

class ModelGateway:
    def __init__(self, providers: dict[str,ModelProvider], *, primary_provider: str="mock",
                 primary_model: str="fixture", fallback_provider: str | None=None,
                 fallback_model: str | None=None, repository=None,
                 transient_statuses=(429,500,502,503,504)):
        self.providers=providers; self.primary_provider=primary_provider; self.primary_model=primary_model
        self.fallback_provider=fallback_provider; self.fallback_model=fallback_model
        self.repository=repository; self.transient_statuses=transient_statuses

    @staticmethod
    def with_ai_identity(messages: list[dict[str,str]], outbound: bool=False):
        system=AI_DISCLOSURE if outbound else "You are an AI employee. Be transparent that you are AI; never impersonate a human."
        return [{"role":"system","content":system},*messages]

    async def _call(self, provider_name, model, messages):
        return await self.providers[provider_name].generate(model=model,messages=messages,temperature=0)

    async def generate_structured(self, *, messages: list[dict[str,str]], output_model: Type[BaseModel],
        tenant_id: str, product_id: str | None, employee_id: str, run_id: str,
        outbound: bool=False, is_sandbox: bool=False,
        estimated_input_cost_inr_per_1k: float | None=None,
        estimated_output_cost_inr_per_1k: float | None=None):
        messages=self.with_ai_identity(messages,outbound)
        if is_sandbox:
            messages=[{"role":"system","content":"SANDBOX MODE: simulate and practise only. Never contact real customers, call production webhooks, trigger financial actions, or mutate production customer records."},*messages]
        started=time.perf_counter(); provider=self.primary_provider; model=self.primary_model
        try:
            response=await self._call(provider,model,messages)
        except (httpx.TimeoutException,httpx.TransportError) as exc:
            if not self.fallback_provider: raise
            provider,model=self.fallback_provider,self.fallback_model or self.primary_model
            response=await self._call(provider,model,messages)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in self.transient_statuses or not self.fallback_provider: raise
            provider,model=self.fallback_provider,self.fallback_model or self.primary_model
            response=await self._call(provider,model,messages)
        prompt=response.prompt_tokens; completion=response.completion_tokens
        try:
            parsed=output_model.model_validate_json(response.content, strict=True) if hasattr(output_model,"model_validate_json") else output_model.parse_raw(response.content)
        except (ValidationError,ValueError,json.JSONDecodeError):
            repair=[*messages,{"role":"assistant","content":response.content},
                    {"role":"user","content":"Repair the previous response. Return ONLY valid JSON matching this schema: "+json.dumps(output_model.model_json_schema() if hasattr(output_model,"model_json_schema") else output_model.schema())}]
            repaired=await self._call(provider,model,repair)
            prompt += repaired.prompt_tokens; completion += repaired.completion_tokens
            response=repaired
            parsed=output_model.model_validate_json(response.content) if hasattr(output_model,"model_validate_json") else output_model.parse_raw(response.content)
        latency=int((time.perf_counter()-started)*1000)
        input_rate=estimated_input_cost_inr_per_1k if estimated_input_cost_inr_per_1k is not None else float(os.getenv("WORKFORCE_INPUT_COST_INR_PER_1K","0"))
        output_rate=estimated_output_cost_inr_per_1k if estimated_output_cost_inr_per_1k is not None else float(os.getenv("WORKFORCE_OUTPUT_COST_INR_PER_1K","0"))
        cost=(prompt*input_rate+completion*output_rate)/1000
        telemetry={"prompt_tokens":prompt,"completion_tokens":completion,"estimated_cost_inr":cost,
            "latency_ms":latency,"provider":response.provider,"model":response.model,
            "tenant_id":tenant_id,"product_id":product_id,"employee_id":employee_id,"run_id":run_id,"is_sandbox":is_sandbox}
        if self.repository is not None:
            await self.repository.record_model_usage(**telemetry)
        return parsed,telemetry
