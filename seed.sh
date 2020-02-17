ENDPOINT=http://localhost:9200

function request {
   if [ -z ${3+x} ]; then
      curl -X $1 "$ENDPOINT/$2" -H 'Content-Type: application/json'
   else
      curl -X $1 "$ENDPOINT/$2" -H 'Content-Type: application/json' -d "$3"
   fi
}

function indices {
   COUNT=$1
   PREFIX=$2
   for i in $(seq 1 $COUNT); do
      INDEX="$PREFIX-$(date -j -v-${i}m "+%Y%m%d")"
      echo $INDEX
      request PUT $INDEX '{"mappings":{"_doc":{"properties":{"message":{"type":"keyword"}}}},"dynamic":false}'
      if [ $i = 1 ]; then
         DATA=$(jq -n --arg index "$INDEX" --arg prefix "$PREFIX" '{actions:[{add:{index:$index,alias:$prefix}}]}')
         request POST _aliases "$DATA"
      fi
      for j in $(seq 1 10); do
         DATE=$(date -j)
         DATA=$(jq -n --arg alias "$ALIAS" --arg date "$DATE" '{post_date:$date,message:$alias}')
         request PUT $INDEX/_doc/$j "$DATA"
      done
   done
}

# Create indices
for alias in logs contents assets; do
   indices 5 $alias
   for i in $(seq 1 10); do
      DATE=$(date -j)
      DATA=$(jq -n --arg alias "$ALIAS" --arg date "$DATE" '{post_date:$date,message:$alias}')
      request PUT $alias/_doc/$i "$DATA"
   done
done
