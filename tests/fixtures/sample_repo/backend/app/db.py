class Store:
    def get(self, key):
        return {}

    def put(self, data):
        return data

    def ping(self) -> bool:
        return True


store = Store()
