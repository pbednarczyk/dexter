## API changes

Every time you add, remove, rename, or modify a public HTTP endpoint:

1. Update the Bruno collection in `bruno/`.
2. Add or update example requests.
3. Update environment variables if required.
4. Update request examples and assertions if the response contract changed.
5. Update README/API documentation if the contract changed.
6. Keep Bruno requests synchronized with actual endpoint paths,
   request bodies and response contracts.

Do not treat Bruno as optional documentation. The Bruno collection is part of
the API contract and must stay in sync with the code.
