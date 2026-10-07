import unittest
import json
import threading
import urllib.error
import urllib.request

import proxy


class FakeSocketClient:
    def __init__(self):
        self.paths = []
        self.ident = "a" * 64

    def get(self, path, limit):
        self.paths.append(path)
        if path == "/containers/json?all=1":
            return [{"Id": self.ident}]
        if path == f"/containers/{self.ident}/json":
            return {"Name": "/background-worker", "Id": self.ident,
                    "Config": {"Env": ["SECRET=hidden"], "Image": "private-image"},
                    "Mounts": [{"Source": "/private"}], "State": {"Status": "running",
                    "StartedAt": "2026-10-07T12:00:00.123456789Z",
                    "Health": {"Status": "healthy", "Log": [{"Output": "sensitive"}]}}}
        raise AssertionError(path)


class ProxyTests(unittest.TestCase):
    def test_only_list_and_inspect_gets_with_filtered_output(self):
        client = FakeSocketClient()
        service = proxy.ProxyService(client, proxy.ProxySettings("status-docker-proxy", False))
        result = service.list_containers()
        self.assertEqual(client.paths, ["/containers/json?all=1", f"/containers/{client.ident}/json"])
        self.assertEqual(set(result[0]), {"name", "state", "health", "state_since"})
        self.assertEqual(result[0]["health"], "healthy")
        for secret in ("SECRET", "private-image", "/private", client.ident, "sensitive"):
            self.assertNotIn(secret, str(result))

    def test_self_exclusion_and_explicit_opt_in(self):
        class OwnContainer(FakeSocketClient):
            def get(self, path, limit):
                result = super().get(path, limit)
                if isinstance(result, dict):
                    result["Name"] = "/status-docker-proxy"
                return result
        client = OwnContainer()
        self.assertEqual(proxy.ProxyService(client, proxy.ProxySettings("status-docker-proxy", False)).list_containers(), [])
        self.assertEqual(len(proxy.ProxyService(client, proxy.ProxySettings("status-docker-proxy", True)).list_containers()), 1)

    def test_proxy_configuration_validation(self):
        self.assertEqual(proxy.ProxySettings.from_env({}).self_name, "status-docker-proxy")
        with self.assertRaises(ValueError):
            proxy.ProxySettings.from_env({"INCLUDE_SELF": "maybe"})

    def test_http_boundary_rejects_raw_api_and_writes(self):
        server = proxy.ProxyHTTPServer(proxy.ProxyService(FakeSocketClient(),
                                      proxy.ProxySettings("status-docker-proxy", False)), ("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/containers", timeout=3) as response:
                data = json.load(response)
            self.assertEqual(set(data["containers"][0]), {"name", "state", "health", "state_since"})
            for path, method, code in (("/containers/json?all=1", "GET", 404),
                                       ("/images/json", "GET", 404), ("/containers", "POST", 405)):
                with self.subTest(path=path, method=method), self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(base + path, method=method), timeout=3)
                self.assertEqual(error.exception.code, code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
