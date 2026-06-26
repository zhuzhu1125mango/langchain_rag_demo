"""Ollama 流式输出调试脚本。

直接调用本地 Ollama 模型进行流式生成，将每个 chunk 写入日志文件用于排查流式行为。
"""

from langchain_ollama import ChatOllama
import sys

llm = ChatOllama(model='deepseek-r1:7b-qwen-distill-q4_K_M', streaming=True)
prompt = '你好，请用一句话回答'
with open('ollama_stream_test.log', 'w', encoding='utf-8') as f:
    f.write('Testing stream...\n')
    f.flush()
    try:
        for i, chunk in enumerate(llm.stream(prompt)):
            f.write(f'Chunk {i}: repr={repr(chunk.content)}\n')
            f.flush()
            if i > 50:
                break
        f.write('Done\n')
    except Exception as e:
        f.write(f'Error: {e}\n')
        import traceback
        traceback.print_exc(file=f)
