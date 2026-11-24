"""Release の署名の検証に使う公開鍵（Ed25519・raw 32 バイトを base64 にしたもの）。

公開してよい値（秘密鍵ではない）。秘密鍵は Release を作る人の手元にだけあり、このリポジトリには入れない（docs/UPDATE.md）。
鍵を替えるときは、この値を新しい公開鍵にした版を、古い鍵で署名して出す（その次の版から新しい鍵で署名する）。
"""
PUBLIC_KEY_B64 = "Q1HpsgKn3FgwoZDv6xKxRhx/7K0LUgFFwunk5k8Ol8E="
