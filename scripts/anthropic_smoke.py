import os

from anthropic import Anthropic
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=os.environ["ANTHROPIC_MODEL"],
        max_tokens=32,
        messages=[{"role": "user", "content": "Reply with: Anthropic connection OK"}],
    )
    print(response.content[0].text)


if __name__ == "__main__":
    main()