"""Start the dbcopy web dashboard:  python main.py"""

import uvicorn


def main():
    print("dbcopy dashboard -> http://0.0.0.0:8000 (access from any network)")
    print("                 -> http://localhost:8000 (local access)")
    uvicorn.run("app:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
