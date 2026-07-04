# typescript

Add and search memories from a TypeScript/Node script using the `hotmem` TS
client against a running sidecar.

## Setup

```sh
hotmem serve                      # start the sidecar on http://127.0.0.1:8711
npm install hotmem                # or: npm install -e ../ts  (local build)
npm install -D tsx                # to run .ts directly
```

## Run

```sh
npx tsx agent.ts
```
