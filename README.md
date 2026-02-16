# kalshi-arb

## Setup details I'm throwing together in VS Code rn
- Use `uv` as your Python package manager (download if you don't have it)
    - `pyproject.toml` contains all project dependencies
    - Running `uv sync` will give you the needed dependencies ez
    - Use `uv add [name]` if you need a new package (will update the .toml)
- Your `.env` file should look like this:
    ```
    # defaults for our runs (real markets, no active trading yet)
    KALSHI_ENV=prod
    KALSHI_EXECUTION_MODE=OBSERVE

    # demo credentials
    KALSHI_DEMO_KEY_ID=[i almost forgot to delete mine from this readme when committing ts]
    KALSHI_DEMO_KEY_PATH=.secrets/demo.txt

    # production credentials
    KALSHI_PROD_KEY_ID=[another string of alphanumeric characters]
    KALSHI_PROD_KEY_PATH=.secrets/prod.txt
    ```
- API setup
    - Get both prod and demo keys in account/security settings (demo url: https://demo.kalshi.co)
    - Just nickname them prod and demo so your files are `prod.txt` and `demo.txt`
    - Put the .txt files under a gitignored folder called `.secrets`
    - The `KEY_ID` is copypasteable from Kalshi settings, put them in the `.env`
    - Pls make sure everything is gitignored properly or I lowk steal your bank account

## Project code guidelines

- Everything that matters is under the `src` directory 
- We're modularizing everything into subdirectories for a reason, it's more maintainable and organized
- Seperate & simplify components as much as you can, market-making project bloated to like a 1500-line `main.py` it was cooked
- It's best to leave brief comments under both functions & important code blocks so everyone understands your code and knows what does what (important for debugging)
- LLMs are a second resort to reading docs. It's obv useful when on a leash but will bloat the codebase into a mess without clear guidance
- Don't let tech debt accumulate. Read this: https://www.ibm.com/think/topics/technical-debt
- Try to make small, iterative code changes and review/cleanup every change you make before continuing