"""Start the dbcopy web dashboard:  python main.py"""

import uvicorn


def main():
    print("dbcopy dashboard -> http://127.0.0.1:8000")
    uvicorn.run("app:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
