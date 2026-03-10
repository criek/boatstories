#run as e.g. sh process.sh ../ethera/ethera.data

python ../data/myshiptrackingconverter.py ${1}
npx ts-node intersections.ts ${1}.line.geojson environment.geojson ${1}-intersection.geojson --track ${1}.points.geojson