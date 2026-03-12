"""Tests for A6: chain selector — config parsing, known/unknown chains."""
import unittest

from knarr.core.chain import get_chain_config, KNOWN_CHAINS


class TestChainSelectorDefaults(unittest.TestCase):

    def test_default_chain_is_devnet(self):
        cfg = get_chain_config({})
        self.assertEqual(cfg["chain_id"], "solana-devnet")

    def test_devnet_defaults(self):
        cfg = get_chain_config({"blockchain": {"chain": "solana-devnet"}})
        self.assertEqual(cfg["chain_id"], "solana-devnet")
        self.assertIn("devnet", cfg["rpc_url"])
        self.assertEqual(cfg["commitment"], "confirmed")

    def test_mainnet_defaults(self):
        cfg = get_chain_config({"blockchain": {"chain": "solana-mainnet"}})
        self.assertEqual(cfg["chain_id"], "solana-mainnet")
        self.assertIn("mainnet-beta", cfg["rpc_url"])
        self.assertEqual(cfg["commitment"], "finalized")

    def test_testnet_defaults(self):
        cfg = get_chain_config({"blockchain": {"chain": "solana-testnet"}})
        self.assertEqual(cfg["chain_id"], "solana-testnet")
        self.assertIn("testnet", cfg["rpc_url"])


class TestChainSelectorOverrides(unittest.TestCase):

    def test_operator_can_override_rpc_url(self):
        cfg = get_chain_config({
            "blockchain": {
                "chain": "solana-devnet",
                "networks": {
                    "solana-devnet": {
                        "rpc_url": "https://my-custom-rpc.example.com",
                    }
                }
            }
        })
        self.assertEqual(cfg["rpc_url"], "https://my-custom-rpc.example.com")
        self.assertEqual(cfg["commitment"], "confirmed")  # default preserved

    def test_operator_can_override_commitment(self):
        cfg = get_chain_config({
            "blockchain": {
                "chain": "solana-devnet",
                "networks": {
                    "solana-devnet": {
                        "commitment": "finalized",
                    }
                }
            }
        })
        self.assertEqual(cfg["commitment"], "finalized")

    def test_operator_can_set_token_mint(self):
        cfg = get_chain_config({
            "blockchain": {
                "chain": "solana-mainnet",
                "networks": {
                    "solana-mainnet": {
                        "token_mint": "KNARRxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                    }
                }
            }
        })
        self.assertEqual(cfg["token_mint"], "KNARRxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


class TestUnknownChainRejected(unittest.TestCase):

    def test_unknown_chain_raises(self):
        with self.assertRaises(ValueError) as ctx:
            get_chain_config({"blockchain": {"chain": "ethereum-mainnet"}})
        self.assertIn("Unknown blockchain chain", str(ctx.exception))

    def test_empty_chain_raises(self):
        with self.assertRaises(ValueError):
            get_chain_config({"blockchain": {"chain": ""}})

    def test_known_chains_constant(self):
        self.assertIn("solana-devnet", KNOWN_CHAINS)
        self.assertIn("solana-testnet", KNOWN_CHAINS)
        self.assertIn("solana-mainnet", KNOWN_CHAINS)


class TestChainConfigRPCRouting(unittest.TestCase):

    def test_devnet_rpc_url_present(self):
        cfg = get_chain_config({})
        self.assertTrue(cfg["rpc_url"].startswith("https://"))

    def test_mainnet_rpc_url_different_from_devnet(self):
        dev = get_chain_config({"blockchain": {"chain": "solana-devnet"}})
        main = get_chain_config({"blockchain": {"chain": "solana-mainnet"}})
        self.assertNotEqual(dev["rpc_url"], main["rpc_url"])

    def test_chain_id_in_returned_dict(self):
        cfg = get_chain_config({"blockchain": {"chain": "solana-testnet"}})
        self.assertIn("chain_id", cfg)
        self.assertIn("rpc_url", cfg)
        self.assertIn("commitment", cfg)
        self.assertIn("token_mint", cfg)


if __name__ == "__main__":
    unittest.main()
